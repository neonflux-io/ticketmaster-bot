"""Checkout module - delivery, payment selection, and (optional) final commit."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...strategies.base import _extract_price, _extract_sections
from ...utils.retry import random_human_delay
from . import auth as auth_module

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...strategies.base import TicketCandidate

log = logging.getLogger("ticketmaster-bot")


class CheckoutError(Exception):
    """Raised when checkout cannot proceed."""


async def select_delivery(
    page: Page,
    *,
    preferred: list[str],
    allow_any: bool,
) -> bool:
    """Select a preferred delivery option (mobile/eticket); never blindly pick first."""
    # 1. Try matching by visible label text.
    for keyword in preferred:
        try:
            label = page.locator(
                f"label:has-text('{keyword}')",
            ).first
            if await label.is_visible(timeout=800):
                await label.click()
                log.info("Selected delivery option matching %r", keyword)
                return True
        except Exception:  # noqa: BLE001
            continue

    if not allow_any:
        log.warning(
            "No preferred delivery option matched %s and allow_any=False; "
            "leaving delivery untouched",
            preferred,
        )
        return False

    # 2. Last resort: first delivery radio. Only when allow_any=True.
    selectors = (
        "input[type='radio'][name*='delivery' i]",
        "[data-bdd='delivery-option']",
    )
    for sel in selectors:
        try:
            radio = page.locator(sel).first
            if await radio.is_visible(timeout=1000):
                await radio.check()
                log.warning(
                    "No preferred delivery match - falling back to first option (%s)",
                    sel,
                )
                return True
        except Exception:  # noqa: BLE001
            continue
    log.debug("No delivery selection needed (or unable to detect)")
    return False


async def select_saved_card(page: Page, last_four: str | None) -> bool:
    """Select a saved card ending in last_four. Returns True if selected."""
    if not last_four:
        return False
    try:
        radio = page.locator(
            f"label:has-text('ending in {last_four}'), "
            f"label:has-text('•••• {last_four}'), "
            f"label:has-text('***{last_four}')"
        ).first
        if await radio.is_visible(timeout=2000):
            await radio.click()
            log.info("Selected saved card ending in %s", last_four)
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("Saved card selection failed: %s", exc)
    return False


async def verify_cart_matches(
    page: Page,
    *,
    expected_quantity: int,
    expected_candidate: TicketCandidate | None,
    price_tolerance: float = 0.05,
) -> bool:
    """Read the cart / order summary and verify it matches expectations.

    Returns True on match (or if we can't extract enough info to disprove the
    match - logged as a warning). Returns False on a definitive mismatch.
    """
    try:
        summary = page.locator(
            "[data-bdd='order-summary'], [data-bdd='cart-summary'], "
            "section:has-text('Order Summary')"
        ).first
        await summary.wait_for(state="visible", timeout=4000)
        text = (await summary.inner_text(timeout=2000)) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Could not read order summary for verification (%s) - proceeding cautiously",
            exc,
        )
        return True

    lowered = text.lower()
    found_qty = _extract_quantity(lowered)
    if found_qty is not None and found_qty != expected_quantity:
        log.error(
            "Cart quantity mismatch: expected %d, summary shows %d",
            expected_quantity,
            found_qty,
        )
        return False

    if expected_candidate is None:
        return True

    if expected_candidate.section:
        found_sections = {s.upper() for s in _extract_sections(text)}
        if found_sections and expected_candidate.section.upper() not in found_sections:
            log.error(
                "Cart section mismatch: expected %s, summary shows %s",
                expected_candidate.section,
                sorted(found_sections),
            )
            return False

    if expected_candidate.price is not None:
        found_price = _extract_price(text)
        if found_price is not None:
            allowed = expected_candidate.price * expected_quantity
            # Allow for fees: assert summary price is within tolerance ABOVE base.
            ratio = found_price / max(allowed, 0.01)
            if not (1.0 - price_tolerance <= ratio <= 1.0 + 0.5 + price_tolerance):
                log.error(
                    "Cart price mismatch: expected ~$%.2f (qty=%d), summary $%.2f (ratio=%.2f)",
                    allowed,
                    expected_quantity,
                    found_price,
                    ratio,
                )
                return False

    return True


def _extract_quantity(text: str) -> int | None:
    import re

    match = re.search(r"(\d+)\s*(?:tickets?|seats?)\b", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def run_checkout(
    page: Page,
    *,
    auto_purchase: bool,
    card_last_four: str | None,
    action_delay: tuple[float, float] = (0.5, 2.0),
    preferred_delivery: list[str] | None = None,
    allow_any_delivery: bool = False,
    expected_quantity: int = 0,
    expected_candidate: TicketCandidate | None = None,
    price_tolerance: float = 0.05,
    captcha_timeout_seconds: float = 300,
) -> bool:
    """Execute the checkout flow.

    If auto_purchase is False, stops after selecting delivery/payment so the
    user can review and click Place Order manually. If True, also verifies the
    cart matches expectations before clicking the final 'Place Order' button.
    """
    preferred = preferred_delivery or [
        "mobile entry",
        "mobile ticket",
        "ticketfast",
        "email delivery",
        "e-ticket",
    ]

    await random_human_delay(*action_delay)
    await select_delivery(page, preferred=preferred, allow_any=allow_any_delivery)
    await random_human_delay(*action_delay)

    if card_last_four:
        await select_saved_card(page, card_last_four)
        await random_human_delay(*action_delay)

    if not auto_purchase:
        log.info(
            "auto_purchase=false - stopping here. Please review and click Place Order manually."
        )
        return True

    if expected_quantity > 0:
        matched = await verify_cart_matches(
            page,
            expected_quantity=expected_quantity,
            expected_candidate=expected_candidate,
            price_tolerance=price_tolerance,
        )
        if not matched:
            log.error("Aborting auto-purchase due to cart mismatch")
            return False

    await _accept_terms(page)
    await random_human_delay(*action_delay)

    # TM frequently throws a captcha at the very last step.
    await auth_module.wait_for_human_if_captcha(page, timeout_seconds=captcha_timeout_seconds)

    place_order_selectors = (
        "button:has-text('Place Order')",
        "button:has-text('Submit Order')",
        "button:has-text('Complete Purchase')",
        "[data-bdd='place-order-button']",
    )
    combined = ", ".join(place_order_selectors)
    try:
        btn = page.locator(combined).first
        await btn.wait_for(state="visible", timeout=8000)
        log.info("Clicking Place Order")
        await btn.click()
    except Exception as exc:  # noqa: BLE001
        log.error("Could not find a 'Place Order' button: %s", exc)
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:  # noqa: BLE001
        pass
    return await _verify_order_placed(page)


async def _accept_terms(page: Page) -> None:
    selectors = (
        "input[type='checkbox'][name*='terms' i]",
        "input[type='checkbox'][id*='agree' i]",
        "input[type='checkbox'][aria-label*='agree' i]",
    )
    for sel in selectors:
        try:
            cb = page.locator(sel).first
            if await cb.is_visible(timeout=500) and not await cb.is_checked():
                await cb.check()
        except Exception:  # noqa: BLE001
            continue


async def _verify_order_placed(page: Page) -> bool:
    """Check for an order-confirmation page."""
    url = page.url.lower()
    if "confirmation" in url or "thankyou" in url or "order-confirmation" in url:
        return True
    indicators = (
        "text=/Order Confirmed/i",
        "text=/Thank you for your order/i",
        "text=/Your order is confirmed/i",
        "[data-bdd='order-confirmation']",
    )
    for sel in indicators:
        try:
            if await page.locator(sel).first.is_visible(timeout=3000):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False
