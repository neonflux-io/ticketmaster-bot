"""ticketmaster.sg authentication.

The SG site does *not* run its own login form. It bounces every login
attempt to the same PingFederate OAuth2 surface that ticketmaster.com
uses (host: ``auth.ticketmaster.com``); only the ``client_id``,
``redirect_uri`` and ``visualPresets`` query parameters change. After
the user submits credentials (and clears any captcha / NuData
fingerprint challenge) the OAuth server bounces through
``identity.ticketmaster.sg/exchange``, which sets the SG-scoped
authentication cookies on ``.ticketmaster.sg``.

The OAuth URL itself is *not* something we hardcode. The real OAuth
URL the SG site generates carries a set of server-only tokens
(``placementId``, ``integratorId``, ``intSiteToken``, ``TMUO``,
``deviceId``, ``disableAutoOptIn``) that PingFederate validates;
hardcoding the URL with only the SG-specific client_id / redirect_uri
/ visualPresets values lands on PingFederate's "Modern Accounts Error
Page" instead of the email-entry form. To avoid this we drive the
login by navigating to ``https://ticketmaster.sg/login`` and letting
the site construct the OAuth URL server-side.

This module drives that flow with the same persistent BrowserContext
the rest of the vendor uses, with two SG-specific touches:

* :func:`is_logged_in` validates the SG cookie set
  (``eps_sid``, ``tmpt``, ``TIXPUISID``) rather than the US cookie
  set. ``BID`` alone is *not* enough because the SG site stamps it on
  first visit even for anonymous browsers (per the F7.1 recon doc).
* :func:`wait_for_human_if_captcha` detects **both** the Yii image
  CAPTCHA on ``/ticket/check-captcha/...`` (``img#TicketForm_verifyCode-image``
  + ``input#TicketForm_verifyCode``) and the invisible reCAPTCHA
  Enterprise iframes that appear on both the captcha and login pages
  (site key ``6LcvL3UrAAAAAO_9u8Seiuf-I6F_tP_jSS-zndXV``).

Per-account persistence still happens at the BrowserContext layer
(``launch_persistent_context(user_data_dir=...)``) — the SG cookie set
is preserved across runs the same way the US cookies are.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ...captcha import CaptchaChallenge
from ...humanize.typing import human_type
from ...utils.retry import random_human_delay
from .selectors import locator, try_selector_for

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

    from ...captcha import CaptchaSolverChain
    from ...utils.config_loader import AccountConfig, HumanizeTypingConfig

log = logging.getLogger("ticketmaster-bot")


# OAuth surface for ticketmaster.sg.
#
# The OAuth login *host* is ``auth.ticketmaster.com`` (the same
# PingFederate front-end the US site uses). The client_id,
# redirect_uri and visualPresets values below are the SG-specific
# query parameters we observed during F7.1 recon and re-verified
# against the live site on 2026-05-20 by clicking Sign In on
# ``https://ticketmaster.sg/`` and recording the redirect chain.
#
# However, the **canonical login entry point** is
# ``https://ticketmaster.sg/login``. Visiting ``/login`` 302s through
# ``identity.ticketmaster.sg/sign-in?...`` and then to
# ``auth.ticketmaster.com/as/authorization.oauth2?...`` with a set
# of server-generated tokens (``placementId``, ``integratorId``,
# ``intSiteToken``, ``TMUO``, ``deviceId``, ``disableAutoOptIn``).
# Without those tokens PingFederate renders its generic "Modern
# Accounts Error Page" — which is exactly what happened the first
# time we tried to hardcode the OAuth URL ourselves. Drive the SG
# login by navigating to ``SG_LOGIN_INITIATE_URL`` and letting the
# site build the OAuth URL server-side.
SG_OAUTH_AUTHORIZE_URL = "https://auth.ticketmaster.com/as/authorization.oauth2"
SG_OAUTH_CLIENT_ID = "1a554b2c04dc.web.ticketmaster.sg"
SG_OAUTH_REDIRECT_URI = "https://identity.ticketmaster.sg/exchange"
SG_OAUTH_VISUAL_PRESETS = "tmsg"
SG_OAUTH_SCOPES = "openid profile phone email tm"
SG_OAUTH_LANG = "en-sg"

SG_HOME_URL = "https://ticketmaster.sg/"
SG_PROFILE_URL = "https://ticketmaster.sg/profile"
SG_LOGIN_INITIATE_URL = "https://ticketmaster.sg/login"

# Cookies the SG site stamps on a logged-in browser.
# ``BID`` is deliberately omitted — the recon doc records it as being
# set even on an anonymous first visit, so its presence cannot
# distinguish "logged in" from "visited the site once". The remaining
# four are only ever observed *after* a successful login.
SG_AUTH_COOKIE_NAMES = frozenset(
    {
        "eps_sid",
        "tmpt",
        "TIXPUISID",
        "_csrf",
    }
)

# A "strong" auth signal: every cookie in this subset must be present.
# Used by ``is_logged_in`` to avoid false-positives when ``_csrf`` is
# set early in the flow.
SG_AUTH_REQUIRED_COOKIES = frozenset({"eps_sid", "tmpt", "TIXPUISID"})

# Hostnames that participate in the SG auth flow. ``identity.ticketmaster.sg``
# is the OAuth exchange target; ``auth.ticketmaster.com`` is the
# PingFederate front-end shared with the US site.
SG_AUTH_FLOW_HOSTS = ("auth.ticketmaster.com", "identity.ticketmaster.sg")


class AuthError(Exception):
    """Raised when SG login fails irrecoverably."""


# ---------------------------------------------------------------------------
# OAuth URL builder
# ---------------------------------------------------------------------------


def build_sg_oauth_url(*, lang: str = SG_OAUTH_LANG) -> str:
    """Return the *fixed-parameter* portion of the SG OAuth authorize URL.

    **Not** safe to navigate to directly. The live SG site appends a
    set of server-generated tokens (``placementId``, ``integratorId``,
    ``intSiteToken``, ``TMUO``, ``deviceId``, ``disableAutoOptIn``)
    to every real OAuth bounce, and the PingFederate front-end at
    ``auth.ticketmaster.com`` returns its generic "Modern Accounts
    Error Page" if those tokens are missing.

    This helper is retained for two purposes only:

    * Sanity-checking the canonical host / client_id / redirect_uri
      / visualPresets combination in tests.
    * Debug logging — printing the "shape" of the OAuth URL the SG
      site is expected to bounce through.

    To actually drive the SG login, navigate to
    :data:`SG_LOGIN_INITIATE_URL` (``https://ticketmaster.sg/login``)
    and let the site construct the OAuth URL server-side. The
    :func:`login` function in this module uses that path.
    """
    params = {
        "client_id": SG_OAUTH_CLIENT_ID,
        "response_type": "code",
        "scope": SG_OAUTH_SCOPES,
        "redirect_uri": SG_OAUTH_REDIRECT_URI,
        "visualPresets": SG_OAUTH_VISUAL_PRESETS,
        "lang": lang,
    }
    return f"{SG_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Captcha / challenge detection
# ---------------------------------------------------------------------------


async def _yii_captcha_visible(page: Page) -> bool:
    """Async DOM probe: is the Yii image CAPTCHA currently in the DOM?"""
    try:
        return await locator(page, "captcha_image").count() > 0
    except Exception:  # noqa: BLE001
        return False


async def _recaptcha_iframe_present(page: Page) -> bool:
    """Async DOM probe: is an invisible reCAPTCHA Enterprise iframe mounted?

    The SG check-captcha and login pages both mount a
    ``<iframe title="reCAPTCHA">`` for the Enterprise widget. The
    iframe stays width=0/height=0/visibility:hidden so we cannot use
    ``is_visible``; presence in the DOM (count > 0) is the signal.
    """
    sel = try_selector_for("captcha_iframe")
    if sel is None:
        return False
    try:
        if await page.locator(sel).count() > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    # Frame URLs also expose the captcha vendor even if the iframe has
    # not been picked up by the page's DOM yet.
    for frame in page.frames:
        url = (frame.url or "").lower()
        if (
            "recaptcha/enterprise" in url
            or "recaptcha/api2/anchor" in url
            or "recaptcha.net/recaptcha" in url
            or "google.com/recaptcha" in url
        ):
            return True
    return False


async def has_captcha(page: Page) -> bool:
    """Return True if either SG captcha gate is currently mounted."""
    return await _yii_captcha_visible(page) or await _recaptcha_iframe_present(page)


# ---------------------------------------------------------------------------
# Cookie-based login probe
# ---------------------------------------------------------------------------


async def _has_required_sg_cookies(context: BrowserContext) -> bool:
    """Return True if the persistent context carries every required SG cookie.

    See :data:`SG_AUTH_REQUIRED_COOKIES` for the set. Cookies are
    queried for the ticketmaster.sg origin only; cookies set on the
    auth.ticketmaster.com or identity.ticketmaster.sg hosts (which
    Ticketmaster sometimes uses for SSO state) are intentionally
    ignored — we want a *site* session, not just a "started login" hint.
    """
    try:
        cookies = await context.cookies("https://ticketmaster.sg/")
    except Exception:  # noqa: BLE001
        return False
    present = {c.get("name") for c in cookies if c.get("name")}
    return SG_AUTH_REQUIRED_COOKIES.issubset(present)


async def is_logged_in(context: BrowserContext) -> bool:
    """Check whether the persistent context holds a valid SG session.

    Heuristic combines two real signals:

    1. The SG-scoped cookie set (:data:`SG_AUTH_REQUIRED_COOKIES`) is
       present after navigating to the SG home page (the navigation
       gives the server a chance to refresh the cookies).
    2. The login flow is *not* currently in progress (we are not on
       ``auth.ticketmaster.com`` or ``identity.ticketmaster.sg``).
    """
    page = await context.new_page()
    try:
        await page.goto(SG_HOME_URL, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass

        url = (page.url or "").lower()
        for host in SG_AUTH_FLOW_HOSTS:
            if host in url:
                return False

        if await has_captcha(page):
            log.debug("is_logged_in: captcha challenge in the way; treating as logged out")
            return False

        if not await _has_required_sg_cookies(context):
            return False

        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("SG is_logged_in check failed: %s", exc)
        return False
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


async def _enter_credential(
    target,  # noqa: ANN001 - Playwright Locator
    value: str,
    *,
    typing_cfg: HumanizeTypingConfig | None,
) -> None:
    """Fill a credential field, optionally with human-cadence typing."""
    if typing_cfg is not None and typing_cfg.enabled:
        try:
            await target.fill("")
        except Exception:  # noqa: BLE001
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
    *,
    captcha_timeout_seconds: float = 300.0,
    captcha_solver_chain: CaptchaSolverChain | None = None,
    captcha_refresh_between_attempts: bool = True,
) -> None:
    """Perform the SG OAuth login flow inside ``context``.

    The flow is:

    1. Navigate to :data:`SG_LOGIN_INITIATE_URL`
       (``https://ticketmaster.sg/login``). The SG site 302s through
       ``identity.ticketmaster.sg/sign-in`` and on to
       ``auth.ticketmaster.com/as/authorization.oauth2?...`` with the
       full set of server-generated tokens (``placementId``,
       ``integratorId``, ``intSiteToken``, ``TMUO``, ``deviceId``,
       ``disableAutoOptIn``). Hardcoding the OAuth URL ourselves
       drops those tokens and lands on PingFederate's "Modern
       Accounts Error Page" — so we always let the site build it.
    2. Fill ``input#email-input`` (selector
       :data:`config/selectors/ticketmaster_sg.yaml::login_email_input`)
       and click ``button[name='sign-in']``.
    3. PingFederate transitions to the password step. We wait for any
       password ``input`` to appear (the email-step ``<input type="email">``
       is replaced) and fill that with the account password.
    4. If a captcha (Yii image OR invisible reCAPTCHA) is mounted,
       pause for the human to solve it; otherwise submit immediately.
    5. Wait for the browser to land back on a ``ticketmaster.sg`` URL
       and confirm the SG cookies are set.
    """
    page = await context.new_page()
    try:
        log.info(
            "Initiating ticketmaster.sg login via %s (client_id=%s)",
            SG_LOGIN_INITIATE_URL,
            SG_OAUTH_CLIENT_ID,
        )
        await page.goto(SG_LOGIN_INITIATE_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        await random_human_delay(*action_delay)

        # ---- email step ---------------------------------------------------
        email_locator = locator(page, "login_email_input")
        await email_locator.wait_for(state="visible", timeout=30000)
        log.info("Entering SG email for account %s", account.name)
        await _enter_credential(email_locator, account.email, typing_cfg=typing_cfg)
        await random_human_delay(*action_delay)

        continue_btn = locator(page, "login_continue_button")
        # PingFederate disables the button until the email field is
        # touched; the fill/type above has already triggered the
        # ``input`` events that re-enable it.
        await continue_btn.click()

        # ---- password step ------------------------------------------------
        # PingFederate transitions in-place: the email <input> is
        # swapped for a password <input type="password">. We wait for
        # the new field to be visible.
        password_locator = page.locator("input[type='password']").first
        try:
            await password_locator.wait_for(state="visible", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            # On some SG flows the password step also requires
            # solving a captcha; surface that with a clear error so
            # the caller can decide whether to pause.
            raise AuthError(
                f"Password step did not appear within 30s (captcha may be blocking): {exc}"
            ) from exc

        log.info("Entering SG password for account %s", account.name)
        await _enter_credential(password_locator, account.password, typing_cfg=typing_cfg)
        await random_human_delay(*action_delay)

        # If a captcha (image or invisible reCAPTCHA) is mounted on
        # this page, give the human time to solve it before we click
        # submit. The SG OAuth page mounts the invisible reCAPTCHA on
        # every load; the user only sees a challenge when Google's
        # risk score is high.
        if await has_captcha(page):
            log.warning("Captcha mounted on password step - waiting for solve")
            await wait_for_human_if_captcha(
                page,
                timeout_seconds=captcha_timeout_seconds,
                captcha_solver_chain=captcha_solver_chain,
                refresh_between_attempts=captcha_refresh_between_attempts,
            )

        # Submit either via the "Sign In" button if present, or by
        # pressing Enter on the password field. PingFederate's button
        # text changes between flows, so we prefer keyboard submit
        # which is consistent.
        await password_locator.press("Enter")

        # Wait for the OAuth bounce to complete (either via
        # identity.ticketmaster.sg/exchange or directly to
        # ticketmaster.sg). Allow extra time in case Google's reCAPTCHA
        # silent challenge runs after submission.
        await _wait_for_login_complete(page, timeout_seconds=captcha_timeout_seconds)

        # Refresh cookie store before checking — Playwright sometimes
        # lags behind on Set-Cookie headers fired during navigation.
        if not await _has_required_sg_cookies(context):
            raise AuthError(
                "SG OAuth flow finished but required cookies are missing "
                f"(expected at least {sorted(SG_AUTH_REQUIRED_COOKIES)})"
            )
        log.info("SG login successful for %s", account.name)
    finally:
        await page.close()


async def _wait_for_login_complete(page: Page, *, timeout_seconds: float = 300.0) -> None:
    """Wait until the browser leaves the auth hosts and lands on ticketmaster.sg."""
    deadline = asyncio.get_event_loop().time() + max(30.0, timeout_seconds)
    captcha_announced = False
    while asyncio.get_event_loop().time() < deadline:
        url = (page.url or "").lower()
        on_auth_host = any(host in url for host in SG_AUTH_FLOW_HOSTS)
        on_sg_site = "ticketmaster.sg" in url and "identity.ticketmaster.sg" not in url

        if on_sg_site and not on_auth_host:
            return

        if await has_captcha(page):
            if not captcha_announced:
                log.warning("Captcha / bot-check detected during SG login - solve in the browser")
                captcha_announced = True
        await asyncio.sleep(1.0)

    raise AuthError("SG login did not complete within timeout")


async def _refresh_yii_captcha(page: Page) -> None:
    """Click the Yii captcha image to rotate it via ``/ticket/captcha?refresh=1``.

    The live SG captcha page mounts an ``onclick`` on
    ``img#TicketForm_verifyCode-image`` that fetches a fresh image
    server-side. Clicking the image is therefore the canonical "rotate
    the challenge" interaction; no DOM-mutation barrier blocks the
    chain between attempts.
    """
    try:
        image = locator(page, "captcha_image")
        await image.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        log.warning("SG captcha refresh click failed: %s", exc)


async def _try_solve_yii_captcha(
    page: Page,
    chain: CaptchaSolverChain,
    *,
    refresh_between_attempts: bool,
) -> bool:
    """Run the solver chain against the currently-mounted Yii captcha.

    Returns ``True`` when the chain produced a solution, the answer
    was typed into ``input#TicketForm_verifyCode``, the submit button
    was clicked, and the page transitioned away from the captcha
    (either via URL change or by clearing the Yii image). Returns
    ``False`` when the chain exhausted without an answer, when the
    captcha element was no longer present mid-flow, or when the
    submit click did not advance the page.
    """
    image_locator = locator(page, "captcha_image")
    input_locator = locator(page, "captcha_input")
    submit_locator = locator(page, "captcha_submit_button")

    try:
        if await image_locator.count() == 0:
            return False
        screenshot = await image_locator.screenshot()
    except Exception as exc:  # noqa: BLE001
        log.warning("SG captcha screenshot failed; falling through to human pause: %s", exc)
        return False

    async def _refresh() -> None:
        if refresh_between_attempts:
            await _refresh_yii_captcha(page)

    challenge = CaptchaChallenge(
        challenge_type="yii_image",
        screenshot_bytes=screenshot,
        refresh_callable=_refresh,
        input_locator=input_locator,
        page=page,
    )

    pre_url = page.url or ""
    solution = await chain.solve_with_chain(challenge)
    if solution is None:
        log.info("SG captcha chain exhausted without producing an answer")
        return False

    try:
        await input_locator.fill("")
    except Exception:  # noqa: BLE001
        pass
    try:
        await input_locator.fill(solution.text)
    except Exception as exc:  # noqa: BLE001
        log.warning("SG captcha input fill failed: %s", exc)
        return False

    try:
        await submit_locator.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        log.warning("SG captcha submit click failed: %s", exc)
        return False

    # Wait briefly for the page to either navigate (URL change) or to
    # render a fresh challenge. The deadline is short here because the
    # caller's overall timeout governs the human-pause fallback.
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        if (page.url or "") != pre_url:
            log.info(
                "SG captcha auto-solved by %s in %dms (URL transition)",
                solution.provider,
                solution.latency_ms,
            )
            return True
        if not await has_captcha(page):
            log.info(
                "SG captcha auto-solved by %s in %dms (challenge cleared)",
                solution.provider,
                solution.latency_ms,
            )
            return True
        await asyncio.sleep(0.5)

    log.info(
        "SG captcha submit by %s did not advance the page within 10s",
        solution.provider,
    )
    return False


async def wait_for_human_if_captcha(
    page: Page,
    *,
    timeout_seconds: float = 300.0,
    captcha_solver_chain: CaptchaSolverChain | None = None,
    refresh_between_attempts: bool = True,
) -> bool:
    """Pause execution while an SG captcha is mounted.

    Returns ``True`` if the page no longer carries a captcha (either
    never did, the chain auto-solved it, or the human cleared it).
    Returns ``False`` if the timeout expired with a captcha still
    mounted.

    When ``captcha_solver_chain`` is provided and a Yii image captcha
    is detected, the chain is consulted first. On success (chain
    returns a :class:`CaptchaSolution`, the input is filled, the
    submit button is clicked, and the page advances) this function
    returns ``True`` immediately. If the chain exhausts without an
    answer, or the submit click does not advance the page, control
    falls through to the existing human-pause behaviour so a human can
    finish the challenge in the browser.
    """
    if not await has_captcha(page):
        return True

    if captcha_solver_chain is not None and await _yii_captcha_visible(page):
        solved = await _try_solve_yii_captcha(
            page,
            captcha_solver_chain,
            refresh_between_attempts=refresh_between_attempts,
        )
        if solved:
            return True
        if not await has_captcha(page):
            return True
        log.warning("SG captcha chain did not solve - falling back to human pause")

    log.warning(
        "SG captcha detected - waiting up to %.0fs for human to solve",
        timeout_seconds,
    )
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        if not await has_captcha(page):
            log.info("SG captcha cleared, resuming")
            return True
        await asyncio.sleep(1.5)
    log.error("SG captcha was not solved within %.0fs", timeout_seconds)
    return False


__all__ = [
    "SG_AUTH_COOKIE_NAMES",
    "SG_AUTH_FLOW_HOSTS",
    "SG_AUTH_REQUIRED_COOKIES",
    "SG_HOME_URL",
    "SG_LOGIN_INITIATE_URL",
    "SG_OAUTH_AUTHORIZE_URL",
    "SG_OAUTH_CLIENT_ID",
    "SG_OAUTH_REDIRECT_URI",
    "SG_OAUTH_VISUAL_PRESETS",
    "SG_PROFILE_URL",
    "AuthError",
    "build_sg_oauth_url",
    "has_captcha",
    "is_logged_in",
    "login",
    "wait_for_human_if_captcha",
]
