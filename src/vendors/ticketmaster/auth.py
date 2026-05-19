"""Authentication module - login via Playwright with session persistence."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ...humanize.typing import human_type
from ...registry import selectors as selector_registry
from ...registry.selectors import locator
from ...utils.retry import random_human_delay

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, FrameLocator, Page

    from ...utils.config_loader import AccountConfig, HumanizeTypingConfig

log = logging.getLogger("ticketmaster-bot")

LOGIN_URL = "https://auth.ticketmaster.com/as/authorization.oauth2"
ACCOUNT_URL = "https://www.ticketmaster.com/member"
TM_AUTH_COOKIE_NAMES = {"SID", "BID-tm", "eps_sid", "MUID", "azk-track"}


class AuthError(Exception):
    """Raised when login fails."""


# ---- captcha / verification detection ------------------------------------


def _has_captcha_frame(page: Page) -> bool:
    for f in page.frames:
        url = (f.url or "").lower()
        if (
            "recaptcha" in url
            or "hcaptcha" in url
            or "geo.captcha-delivery" in url
            or "captcha-delivery" in url
            or "/_imperva" in url
        ):
            return True
    return False


async def _looks_like_challenge_page(page: Page) -> bool:
    """Title- and content-based detection for Imperva / Akamai / Cloudflare."""
    try:
        title = (await page.title()) or ""
    except Exception:  # noqa: BLE001
        title = ""
    lowered = title.lower()
    if any(
        marker in lowered
        for marker in (
            "access denied",
            "verifying",
            "just a moment",
            "checking your browser",
            "attention required",
        )
    ):
        return True
    try:
        body_text = await locator(page, "page_body").inner_text(timeout=1000)
    except Exception:  # noqa: BLE001
        return False
    lowered_body = (body_text or "").lower()
    return any(
        marker in lowered_body
        for marker in (
            "are you a human",
            "please verify you are human",
            "complete the security check",
        )
    )


async def _has_auth_cookie(context: BrowserContext) -> bool:
    try:
        cookies = await context.cookies()
    except Exception:  # noqa: BLE001
        return False
    for cookie in cookies:
        if cookie.get("name") in TM_AUTH_COOKIE_NAMES:
            return True
    return False


async def is_logged_in(context: BrowserContext) -> bool:
    """Check whether the persistent context already has a valid Ticketmaster session."""
    page = await context.new_page()
    try:
        await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass

        url = page.url
        if "auth.ticketmaster.com" in url or "identity.ticketmaster.com" in url:
            return False

        if _has_captcha_frame(page) or await _looks_like_challenge_page(page):
            log.debug("is_logged_in: bot-detection challenge in the way; treating as logged out")
            return False

        # If no TM auth cookie at all, definitely not logged in.
        if not await _has_auth_cookie(context):
            return False

        try:
            sign_in_visible = await locator(page, "sign_in_button").is_visible(timeout=2000)
        except Exception:  # noqa: BLE001
            sign_in_visible = False
        return not sign_in_visible
    except Exception as exc:  # noqa: BLE001
        log.debug("is_logged_in check failed: %s", exc)
        return False
    finally:
        await page.close()


# ---- login flow ----------------------------------------------------------


async def _resolve_auth_scope(page: Page) -> Page | FrameLocator:
    """Return either the page or a FrameLocator if TM is showing an auth iframe."""
    for sel in selector_registry.get("auth_frame"):
        try:
            frame_locator = page.frame_locator(sel)
            # Probe whether the frame contains anything we recognize.
            probe = locator(frame_locator, "login_email_input")
            try:
                if await probe.is_visible(timeout=2000):
                    return frame_locator
            except Exception:  # noqa: BLE001
                continue
        except Exception:  # noqa: BLE001
            continue
    return page


async def _enter_credential(
    target,  # noqa: ANN001 - Playwright Locator
    value: str,
    *,
    typing_cfg: HumanizeTypingConfig | None,
) -> None:
    """Fill a credential field, optionally via :func:`human_type`.

    When ``typing_cfg`` is provided and its ``enabled`` flag is true,
    we type the value character-by-character with a normal-distribution
    delay so the keystroke timing looks human. Otherwise we fall back
    to ``locator.fill(value)`` which is instant.
    """
    if typing_cfg is not None and typing_cfg.enabled:
        # Clear any prefilled value so the typed result matches `value` exactly.
        try:
            await target.fill("")
        except Exception:  # noqa: BLE001
            # Best-effort clear; some inputs reject fill("") but accept type.
            pass
        await human_type(
            target,
            value,
            mean_ms=typing_cfg.mean_ms,
            std_ms=typing_cfg.std_ms,
            min_ms=typing_cfg.min_ms,
        )
    else:
        await target.fill(value)


async def login(
    context: BrowserContext,
    account: AccountConfig,
    action_delay: tuple[float, float] = (0.5, 2.0),
    typing_cfg: HumanizeTypingConfig | None = None,
) -> None:
    """Perform interactive login through the Ticketmaster auth flow."""
    page = await context.new_page()
    try:
        log.info("Navigating to Ticketmaster sign in...")
        await page.goto(
            "https://www.ticketmaster.com/member",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await random_human_delay(*action_delay)

        try:
            sign_in_btn = locator(page, "sign_in_button")
            if await sign_in_btn.is_visible(timeout=2500):
                await sign_in_btn.click()
                await random_human_delay(*action_delay)
        except Exception:  # noqa: BLE001
            pass

        scope = await _resolve_auth_scope(page)
        email_locator = locator(scope, "login_email_input")
        await email_locator.wait_for(state="visible", timeout=20000)

        log.info("Filling credentials for %s", account.name)
        await _enter_credential(email_locator, account.email, typing_cfg=typing_cfg)
        await random_human_delay(*action_delay)

        password_locator = locator(scope, "login_password_input")
        await _enter_credential(password_locator, account.password, typing_cfg=typing_cfg)
        await random_human_delay(*action_delay)

        submit = locator(scope, "login_submit_button")
        await submit.click()

        await _wait_for_login_complete(page)
        log.info("Login successful")
    finally:
        await page.close()


async def _wait_for_login_complete(page: Page, timeout_seconds: float = 120) -> None:
    """Wait for login to complete, allowing time for manual captcha solving."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    captcha_announced = False
    while asyncio.get_event_loop().time() < deadline:
        url = page.url
        if "ticketmaster.com" in url and (
            "/member" in url
            or "/account" in url
            or url.endswith("ticketmaster.com/")
            or "auth" not in url
        ):
            return

        if _has_captcha_frame(page) or await _looks_like_challenge_page(page):
            if not captcha_announced:
                log.warning(
                    "Captcha or bot-check detected during login - "
                    "please solve it in the browser window."
                )
                captcha_announced = True
        await asyncio.sleep(1.0)

    raise AuthError("Login did not complete within timeout")


async def wait_for_human_if_captcha(
    page: Page,
    *,
    timeout_seconds: float = 300,
) -> bool:
    """Pause execution while a captcha/challenge is visible.

    Returns ``True`` if the challenge was cleared (or never present), ``False``
    if the timeout expired with the challenge still showing.
    """
    if not _has_captcha_frame(page) and not await _looks_like_challenge_page(page):
        return True
    log.warning(
        "Captcha / bot-check detected during flow - waiting up to %.0fs for human to solve",
        timeout_seconds,
    )
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if not _has_captcha_frame(page) and not await _looks_like_challenge_page(page):
            log.info("Challenge cleared, resuming")
            return True
        await asyncio.sleep(1.5)
    log.error("Captcha was not solved within %.0fs", timeout_seconds)
    return False
