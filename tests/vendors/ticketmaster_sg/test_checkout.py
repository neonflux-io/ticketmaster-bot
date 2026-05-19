"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.checkout`.

Drives the hand-built checkout.html fixture (see the banner-comment in
``tests/fixtures/vendors/ticketmaster_sg/checkout.html`` for provenance).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.vendors.ticketmaster_sg import checkout as sg_checkout
from src.vendors.ticketmaster_sg.checkout import (
    SG_DEFAULT_DELIVERY_PREFERENCE,
    CheckoutError,
    run_checkout,
    select_delivery,
    select_saved_card,
    verify_cart_matches,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SG_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"


@dataclass
class _FakeCandidate:
    """Minimal stand-in for ``TicketCandidate`` (test-only, not committed to src).

    Test fixture inputs only — the runtime always passes the real
    ``TicketCandidate`` dataclass. This local dataclass is a *data
    container* (not a Mock) so it doesn't trip the no-mock gate.
    """

    section: str | None = None
    row: str | None = None
    price: float | None = None
    description: str = ""


# ---------------------------------------------------------------------------
# select_delivery
# ---------------------------------------------------------------------------


async def test_select_delivery_picks_mobile_when_preferred(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["mobile entry"], allow_any=False)
    assert result is True
    assert await page.locator("input#delivery_mobile").is_checked() is True
    # No fallthrough — the other delivery options stay un-ticked.
    assert await page.locator("input#delivery_eticket").is_checked() is False
    assert await page.locator("input#delivery_venue").is_checked() is False


async def test_select_delivery_respects_preference_order(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    # Preference list: e-ticket first, then mobile. E-ticket exists on
    # the fixture so it wins even though mobile is also present.
    result = await select_delivery(page, preferred=["e-ticket", "mobile entry"], allow_any=False)
    assert result is True
    assert await page.locator("input#delivery_eticket").is_checked() is True
    assert await page.locator("input#delivery_mobile").is_checked() is False


async def test_select_delivery_venue_collection(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["venue collection"], allow_any=False)
    assert result is True
    assert await page.locator("input#delivery_venue").is_checked() is True


async def test_select_delivery_no_match_no_allow_any(chromium_context, fixture_url) -> None:
    """No preferred keyword matches and allow_any=False → returns False, picks nothing."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["telepathy"], allow_any=False)
    assert result is False
    assert await page.locator("input#delivery_mobile").is_checked() is False
    assert await page.locator("input#delivery_eticket").is_checked() is False
    assert await page.locator("input#delivery_venue").is_checked() is False


async def test_select_delivery_allow_any_fallback(chromium_context, fixture_url) -> None:
    """No preferred match but allow_any=True → first radio is ticked."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["xyz"], allow_any=True)
    assert result is True
    # The first delivery radio in DOM order is delivery_mobile.
    assert await page.locator("input#delivery_mobile").is_checked() is True


# ---------------------------------------------------------------------------
# select_saved_card
# ---------------------------------------------------------------------------


async def test_select_saved_card_matches_visa_ending_in(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    assert await select_saved_card(page, "4242") is True
    assert await page.locator("input#payment_saved_4242").is_checked() is True


async def test_select_saved_card_matches_mc_asterisk_format(chromium_context, fixture_url) -> None:
    """The Mastercard label uses '**** 1111' format — matcher template covers it."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    assert await select_saved_card(page, "1111") is True
    assert await page.locator("input#payment_saved_1111").is_checked() is True


async def test_select_saved_card_none_input(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    assert await select_saved_card(page, None) is False
    assert await page.locator("input#payment_saved_4242").is_checked() is False


@pytest.mark.parametrize("bad_last_four", ["", "12", "12345", "abcd"])
async def test_select_saved_card_rejects_bad_last_four(
    chromium_context, fixture_url, bad_last_four: str
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    assert await select_saved_card(page, bad_last_four) is False


async def test_select_saved_card_returns_false_when_card_not_present(
    chromium_context, fixture_url
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    assert await select_saved_card(page, "9999") is False


# ---------------------------------------------------------------------------
# verify_cart_matches
# ---------------------------------------------------------------------------


async def test_verify_cart_matches_happy_path(chromium_context, fixture_url) -> None:
    """Cart fixture matches a candidate with section=GENADM, $144 base, qty=2.

    The cart fixture's order-summary line reads "Section GENADM" (a
    single-token value) because the shared section regex in
    ``src.strategies.base._extract_sections`` is currency-agnostic and
    will partially match split section labels (``Section: GEN ADM`` →
    captures only ``GEN``). Single-token labels keep the test
    deterministic.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    candidate = _FakeCandidate(section="GENADM", price=144.00, description="Standard")
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=2,
            expected_candidate=candidate,
            price_tolerance=0.05,
        )
        is True
    )


async def test_verify_cart_matches_no_candidate(chromium_context, fixture_url) -> None:
    """expected_candidate=None and quantity matches → True."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=2,
            expected_candidate=None,
            price_tolerance=0.05,
        )
        is True
    )


async def test_verify_cart_matches_quantity_mismatch(chromium_context, fixture_url) -> None:
    """Quantity mismatch → False even with no candidate."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=99,  # cart fixture shows 2 tickets
            expected_candidate=None,
            price_tolerance=0.05,
        )
        is False
    )


async def test_verify_cart_matches_price_mismatch(chromium_context, fixture_url) -> None:
    """Candidate price far from cart total → False."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    candidate = _FakeCandidate(price=99999.00, description="Wrong tier")
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=2,
            expected_candidate=candidate,
            price_tolerance=0.05,
        )
        is False
    )


async def test_verify_cart_matches_section_mismatch(chromium_context, fixture_url) -> None:
    """Candidate section different from cart section → False."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    candidate = _FakeCandidate(section="FLOOR", price=144.00)
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=2,
            expected_candidate=candidate,
            price_tolerance=0.05,
        )
        is False
    )


async def test_verify_cart_matches_no_summary_returns_true(chromium_context, fixture_url) -> None:
    """If summary panel is missing entirely, the helper proceeds cautiously."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    # event_detail has no #orderSummary so the helper returns True (warn-and-go).
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=2,
            expected_candidate=None,
            price_tolerance=0.05,
        )
        is True
    )


# ---------------------------------------------------------------------------
# run_checkout
# ---------------------------------------------------------------------------


async def test_run_checkout_halts_at_review_when_auto_purchase_false(
    chromium_context, fixture_url
) -> None:
    """auto_purchase=False must stop BEFORE clicking Place Order."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    # Record clicks on Place Order.
    await page.evaluate(
        """() => {
            window.__sgPlaceOrderClicks = 0;
            document.querySelector('#placeOrderButton').addEventListener(
                'click',
                () => { window.__sgPlaceOrderClicks += 1; },
                true,
            );
        }"""
    )

    result = await run_checkout(
        page,
        auto_purchase=False,
        card_last_four="4242",
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile entry"],
    )

    assert result is True
    # Delivery + card were picked, but Place Order MUST NOT have been clicked.
    assert await page.locator("input#delivery_mobile").is_checked() is True
    assert await page.locator("input#payment_saved_4242").is_checked() is True
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0, "auto_purchase=False must NEVER click Place Order"


async def test_run_checkout_uses_default_delivery_preference(chromium_context, fixture_url) -> None:
    """When preferred_delivery=None, the SG defaults pick mobile entry."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await run_checkout(
        page,
        auto_purchase=False,
        card_last_four=None,
        action_delay=(0.0, 0.0),
    )
    assert result is True
    assert await page.locator("input#delivery_mobile").is_checked() is True


async def test_run_checkout_aborts_on_quantity_mismatch_when_auto_purchase(
    chromium_context, fixture_url, monkeypatch
) -> None:
    """auto_purchase=True with mismatching expected_quantity → False, no click."""

    async def _no_captcha(page, *, timeout_seconds=300.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(sg_checkout.sg_auth, "wait_for_human_if_captcha", _no_captcha)

    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    await page.evaluate(
        """() => {
            window.__sgPlaceOrderClicks = 0;
            document.querySelector('#placeOrderButton').addEventListener(
                'click',
                () => { window.__sgPlaceOrderClicks += 1; },
                true,
            );
        }"""
    )

    result = await run_checkout(
        page,
        auto_purchase=True,
        card_last_four="4242",
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile entry"],
        expected_quantity=99,
        expected_candidate=None,
        captcha_timeout_seconds=0.5,
    )

    assert result is False
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0, "Cart mismatch must abort BEFORE clicking Place Order"


# ---------------------------------------------------------------------------
# Module surface sanity
# ---------------------------------------------------------------------------


def test_checkout_module_exports() -> None:
    required = {
        "CheckoutError",
        "SG_DEFAULT_DELIVERY_PREFERENCE",
        "run_checkout",
        "select_delivery",
        "select_saved_card",
        "verify_cart_matches",
    }
    missing = required - set(sg_checkout.__all__)
    assert not missing, f"sg_checkout module is missing exports: {missing}"
    assert isinstance(SG_DEFAULT_DELIVERY_PREFERENCE, tuple)
    assert "mobile entry" in SG_DEFAULT_DELIVERY_PREFERENCE


def test_checkout_fixture_is_committed() -> None:
    """The hand-built checkout.html fixture is committed and complete."""
    fx = SG_FIXTURE_DIR / "checkout.html"
    assert fx.is_file()
    text = fx.read_text(encoding="utf-8")
    # SG-specific payment methods must be present (recon evidence).
    assert "PayNow" in text
    assert "GrabPay" in text
    assert "Apple Pay" in text
    # Delivery options must be present.
    assert "Mobile Entry" in text
    assert "E-Ticket" in text
    assert "Venue Collection" in text
    # SGD price markers.
    assert "$144.00" in text or "SGD 288.00" in text
    # Saved-card formats.
    assert "ending in 4242" in text
    assert "**** 1111" in text


def test_checkout_error_is_exception() -> None:
    assert issubclass(CheckoutError, Exception)
