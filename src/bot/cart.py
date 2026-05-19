"""Cart module - set quantity and add tickets to cart."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..utils.retry import random_human_delay
from . import auth as auth_module

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")

# Keywords that identify *legitimate* terms-of-purchase checkboxes.
_TERMS_KEYWORDS = (
    "terms",
    "agree",
    "purchase policy",
    "policy",
    "conditions of use",
    "i understand",
)
# Keywords that explicitly identify opt-in / marketing checkboxes we must avoid.
_OPTIN_BLOCKLIST = (
    "newsletter",
    "promotion",
    "marketing",
    "subscribe",
    "offers",
)


class CartError(Exception):
    """Raised when cart operations fail."""


async def set_quantity(page: Page, quantity: int) -> None:
    """Set ticket quantity. TM uses a <select> dropdown."""
    log.info("Setting ticket quantity to %d", quantity)

    combined = ", ".join(
        (
            "select[name='quantity']",
            "select[data-bdd='quantity-select']",
            "select[aria-label*='quantity' i]",
            "select#qty",
        )
    )
    try:
        select = page.locator(combined).first
        await select.wait_for(state="visible", timeout=4000)
        await select.select_option(value=str(quantity))
        return
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Could not find a quantity selector (%s) - quantity may default to 2", exc
        )


async def accept_terms_if_needed(page: Page) -> None:
    """Tick any 'I agree to TM terms' checkbox if present, but never opt-ins."""
    checkbox_selectors = (
        "input[type='checkbox'][name*='terms' i]",
        "input[type='checkbox'][id*='terms' i]",
        "input[type='checkbox'][aria-label*='terms' i]",
        "input[type='checkbox'][name*='agree' i]",
        "input[type='checkbox'][id*='agree' i]",
        "input[type='checkbox'][aria-label*='agree' i]",
    )
    for sel in checkbox_selectors:
        cb = page.locator(sel).first
        try:
            if not await cb.is_visible(timeout=500):
                continue
            label_text = await _checkbox_label_text(cb)
            lowered = label_text.lower()
            if any(b in lowered for b in _OPTIN_BLOCKLIST):
                continue
            if label_text and not any(k in lowered for k in _TERMS_KEYWORDS):
                # Visible label exists but doesn't look like terms - skip.
                continue
            if not await cb.is_checked():
                await cb.check()
                log.debug("Accepted terms via %s (label=%r)", sel, label_text)
        except Exception:  # noqa: BLE001
            continue


async def _checkbox_label_text(checkbox_locator) -> str:  # noqa: ANN001
    """Best-effort fetch the human label associated with a checkbox."""
    candidates = (
        "xpath=ancestor::label[1]",
        "xpath=following-sibling::label[1]",
        "xpath=preceding-sibling::label[1]",
        "xpath=ancestor::*[self::label or self::div][1]",
    )
    for xp in candidates:
        try:
            loc = checkbox_locator.locator(xp)
            if await loc.count() == 0:
                continue
            text = (await loc.first.inner_text(timeout=300)) or ""
            text = text.strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    try:
        aria = await checkbox_locator.get_attribute("aria-label", timeout=300)
        return (aria or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def add_to_cart(
    page: Page,
    *,
    action_delay: tuple[float, float] = (0.5, 2.0),
    timeout_seconds: float = 30,
    captcha_timeout_seconds: float = 300,
) -> bool:
    """Click the primary 'Add to Cart' / 'Continue' button to reserve tickets."""
    await random_human_delay(*action_delay)
    await accept_terms_if_needed(page)
    await random_human_delay(*action_delay)

    # Scope generic "Continue"/"Reserve" buttons to a likely-correct container
    # so we don't click the wrong CTA in unrelated UI chrome.
    scopes = (
        "form:has([data-bdd='quick-picks-list']), "
        "[data-bdd='ticket-list'], "
        "[role='dialog']:has-text('Add to Cart'), "
        "main",
    )
    candidates = (
        "button:has-text('Add to Cart')",
        "[data-bdd='checkout-button']",
        "[data-bdd='add-to-cart-button']",
        f"{scopes} button:has-text('Continue')",
        f"{scopes} button:has-text('Reserve')",
    )
    clicked = False
    combined = ", ".join(candidates)
    try:
        btn = page.locator(combined).first
        await btn.wait_for(state="visible", timeout=int(timeout_seconds * 1000))
        log.info("Clicking primary add-to-cart action")
        await btn.click()
        clicked = True
    except Exception as exc:  # noqa: BLE001
        log.debug("Combined selector failed: %s; trying per-selector", exc)
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    log.info("Clicking primary action: %s", sel)
                    await btn.click()
                    clicked = True
                    break
            except Exception as inner:  # noqa: BLE001
                log.debug("Selector %s failed: %s", sel, inner)

    if not clicked:
        log.error("Could not find an add-to-cart button")
        return False

    try:
        await page.wait_for_load_state(
            "networkidle", timeout=int(timeout_seconds * 1000)
        )
    except Exception:  # noqa: BLE001
        await page.wait_for_timeout(2000)

    # TM frequently re-challenges between add-to-cart and the checkout page.
    await auth_module.wait_for_human_if_captcha(
        page, timeout_seconds=captcha_timeout_seconds
    )

    return await _verify_in_cart(page)


async def _verify_in_cart(page: Page) -> bool:
    """Confirm the cart/checkout page loaded successfully."""
    parsed = urlparse(page.url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if (
        path.startswith("/checkout")
        or "checkout.ticketmaster.com" in host
        or path.startswith("/cart")
    ):
        log.info("Tickets reserved in cart!")
        return True
    indicators = (
        "text=/Order Summary/i",
        "text=/Payment Method/i",
        "text=/Delivery Method/i",
        "[data-bdd='checkout-page']",
    )
    for sel in indicators:
        try:
            if await page.locator(sel).first.is_visible(timeout=2000):
                log.info("Tickets reserved in cart!")
                return True
        except Exception:  # noqa: BLE001
            continue
    log.warning("Add-to-cart click did not transition to checkout page")
    return False
