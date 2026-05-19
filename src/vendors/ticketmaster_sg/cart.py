"""ticketmaster.sg cart module — set quantity, tick terms, add to cart.

The SG cart flow is structurally similar to the US flow handled by
:mod:`src.vendors.ticketmaster.cart` but the DOM is the Yii/Bootstrap
stack the SG site runs (see :mod:`src.vendors.ticketmaster_sg`).  All
DOM lookups route through :mod:`src.vendors.ticketmaster_sg.selectors`
so the inline-selector grep gate stays at zero matches and the YAML at
``config/selectors/ticketmaster_sg.yaml`` remains the single source of
truth.

The four public functions correspond exactly to the F7.4 brief:

* :func:`set_quantity` — drives the
  ``select[name='TicketForm[count]']`` quantity dropdown captured
  during F7.1 recon.
* :func:`accept_terms_if_needed` — ticks the SG terms checkbox
  (``input#TicketForm_agree`` plus the ``CheckoutForm[agree]``
  variant) while explicitly skipping marketing opt-ins
  (``TicketForm[marketingOptIn]`` etc.).
* :func:`add_to_cart` — clicks ``button#autoMode`` (the SG "Best
  Available" trigger) and waits through any second-stage Yii captcha
  + reCAPTCHA the site re-throws between the cart and checkout pages.
* :func:`verify_cart_in_cart` — confirms the browser landed on a
  ``/ticket/checkout/`` URL or rendered one of the cart-page DOM
  indicators.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ...utils.retry import random_human_delay
from . import auth as sg_auth
from . import selectors as sg_selectors

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")

# Marketing / promo opt-ins the cart accept-terms helper must never tick,
# even if their checkbox is registered under the same fallback group as
# the legitimate terms checkbox. Matched as substrings against the
# checkbox's id / name / surrounding label text.
_OPTIN_BLOCKLIST = (
    "newsletter",
    "promotion",
    "marketing",
    "subscribe",
    "offers",
    "marketingoptin",
)

# Keywords that identify a *legitimate* terms-of-purchase checkbox label.
# Matched case-insensitively as substrings against the label/aria-label
# text. The SG site sometimes labels the checkbox simply "I agree"; the
# id (``TicketForm_agree``) is the authoritative signal but we still
# guard against accidental marketing matches via the blocklist above.
_TERMS_KEYWORDS = (
    "terms",
    "agree",
    "purchase",
    "policy",
    "conditions",
    "i understand",
)


class CartError(Exception):
    """Raised when SG cart operations fail irrecoverably."""


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------


async def set_quantity(page: Page, quantity: int) -> bool:
    """Set the ticket quantity on the SG cart / ticket-area page.

    Returns ``True`` if the quantity was set, ``False`` if no quantity
    select was visible (the SG site occasionally pre-fills the quantity
    on the cart page if the user picked it on the area page).
    """
    if quantity < 1:
        raise CartError(f"set_quantity requires a positive integer, got {quantity!r}")

    log.info("Setting SG ticket quantity to %d", quantity)
    try:
        select = sg_selectors.locator(page, "cart_quantity_select")
        await select.wait_for(state="visible", timeout=4000)
        await select.select_option(value=str(quantity))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not find SG quantity selector (%s); leaving quantity untouched", exc)
        return False


# ---------------------------------------------------------------------------
# Terms acceptance
# ---------------------------------------------------------------------------


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
            text = (await loc.first.inner_text(timeout=400)) or ""
            text = text.strip()
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    try:
        aria = await checkbox_locator.get_attribute("aria-label", timeout=400)
        return (aria or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _is_marketing_optin(checkbox_locator) -> bool:  # noqa: ANN001
    """Return True if the checkbox is one of the SG marketing opt-ins."""
    for attr in ("id", "name", "data-purpose"):
        try:
            value = await checkbox_locator.get_attribute(attr, timeout=400)
        except Exception:  # noqa: BLE001
            value = None
        if not value:
            continue
        lowered = value.lower()
        if any(b in lowered for b in _OPTIN_BLOCKLIST):
            return True
    label = (await _checkbox_label_text(checkbox_locator)).lower()
    return any(b in label for b in _OPTIN_BLOCKLIST)


async def accept_terms_if_needed(page: Page) -> bool:
    """Tick the SG terms-of-purchase checkbox, skipping marketing opt-ins.

    Returns ``True`` if at least one terms checkbox was found and
    checked (or was already checked); ``False`` if no terms checkbox
    matched. The function is idempotent — re-ticking an already-checked
    box is a no-op.
    """
    matched = False
    for sel in sg_selectors.get("cart_terms_checkbox"):
        try:
            elements = page.locator(sel)
            count = await elements.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(count):
            cb = elements.nth(i)
            try:
                if not await cb.is_visible(timeout=400):
                    continue
            except Exception:  # noqa: BLE001
                continue

            if await _is_marketing_optin(cb):
                log.debug("Skipping SG marketing opt-in checkbox at %s[%d]", sel, i)
                continue

            label = await _checkbox_label_text(cb)
            lowered = label.lower()
            if label and not any(k in lowered for k in _TERMS_KEYWORDS):
                # Label exists but doesn't look like terms - skip rather
                # than risk ticking the wrong control.
                log.debug(
                    "Skipping SG checkbox at %s[%d] (label=%r not a terms label)",
                    sel,
                    i,
                    label,
                )
                continue

            try:
                if not await cb.is_checked():
                    await cb.check()
                    log.info("Accepted SG terms via %s[%d] (label=%r)", sel, i, label)
                matched = True
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not tick SG terms checkbox at %s[%d]: %s", sel, i, exc)
                continue
    return matched


# ---------------------------------------------------------------------------
# Add to cart
# ---------------------------------------------------------------------------


async def add_to_cart(
    page: Page,
    *,
    action_delay: tuple[float, float] = (0.5, 2.0),
    timeout_seconds: float = 30.0,
    captcha_timeout_seconds: float = 300.0,
) -> bool:
    """Click the SG "Best Available" button to reserve tickets in cart.

    The flow accepts terms (skipping marketing opt-ins), clicks
    ``button#autoMode``, waits for the page to settle, transparently
    pauses for a human to clear any captcha the SG site re-throws
    between cart and checkout, and reports whether the browser
    actually landed on a cart / checkout URL.
    """
    await random_human_delay(*action_delay)
    await accept_terms_if_needed(page)
    await random_human_delay(*action_delay)

    candidates = sg_selectors.get("cart_add_to_cart_button")
    clicked = False
    try:
        btn = sg_selectors.locator(page, "cart_add_to_cart_button")
        await btn.wait_for(state="visible", timeout=int(timeout_seconds * 1000))
        log.info("Clicking SG add-to-cart action (button#autoMode)")
        await btn.click()
        clicked = True
    except Exception as exc:  # noqa: BLE001
        log.debug("Combined SG add-to-cart selector failed: %s; trying per-fallback", exc)
        for sel in candidates:
            try:
                fallback = page.locator(sel).first
                if await fallback.is_visible(timeout=1500):
                    log.info("Clicking SG add-to-cart fallback %s", sel)
                    await fallback.click()
                    clicked = True
                    break
            except Exception as inner:  # noqa: BLE001
                log.debug("SG add-to-cart fallback %s failed: %s", sel, inner)

    if not clicked:
        log.error("Could not click any SG add-to-cart button")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=int(timeout_seconds * 1000))
    except Exception:  # noqa: BLE001
        await page.wait_for_timeout(2000)

    # SG re-throws the Yii / reCAPTCHA challenge between cart and
    # checkout for high-traffic events. Pause for a human if needed.
    await sg_auth.wait_for_human_if_captcha(page, timeout_seconds=captcha_timeout_seconds)

    return await verify_cart_in_cart(page)


# ---------------------------------------------------------------------------
# Cart-page transition probe
# ---------------------------------------------------------------------------


async def verify_cart_in_cart(page: Page) -> bool:
    """Confirm the SG cart / checkout page is now loaded.

    Returns True if either:

    * The page URL contains one of the SG cart-URL markers
      (``/ticket/checkout/`` / ``/cart``), OR
    * Any of the SG cart-page DOM indicators is visible.
    """
    parsed = urlparse(page.url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    url_markers = sg_selectors.get("cart_url_markers")
    if "ticketmaster.sg" in host or host == "":
        for marker in url_markers:
            if marker.lower() in path:
                log.info("SG cart confirmed via URL marker %r (path=%s)", marker, path)
                return True

    for sel in sg_selectors.get("cart_page_indicators"):
        try:
            if await page.locator(sel).first.is_visible(timeout=1500):
                log.info("SG cart confirmed via DOM indicator %s", sel)
                return True
        except Exception:  # noqa: BLE001
            continue

    log.warning("SG add-to-cart did not transition to the cart page (url=%s)", page.url)
    return False


__all__ = [
    "CartError",
    "accept_terms_if_needed",
    "add_to_cart",
    "set_quantity",
    "verify_cart_in_cart",
]
