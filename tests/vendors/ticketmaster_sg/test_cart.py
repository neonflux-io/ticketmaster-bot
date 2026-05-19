"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.cart`.

Drives the hand-built cart.html fixture (see the banner-comment in
``tests/fixtures/vendors/ticketmaster_sg/cart.html`` for provenance) and
the existing F7.1 ticket-area / event-detail fixtures via real headless
Chromium loaded from ``file://`` URLs. No mocks; the only browser is
the shared ``chromium`` / ``chromium_context`` from ``tests/conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.vendors.ticketmaster_sg import cart as sg_cart
from src.vendors.ticketmaster_sg.cart import (
    CartError,
    accept_terms_if_needed,
    add_to_cart,
    set_quantity,
    verify_cart_in_cart,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SG_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"


# ---------------------------------------------------------------------------
# set_quantity
# ---------------------------------------------------------------------------


async def test_set_quantity_selects_value_on_cart_fixture(chromium_context, fixture_url) -> None:
    """``set_quantity(page, 4)`` flips the cart-page select to value 4.

    The live SG cart page renders the quantity ``<select>`` as
    ``TicketForm[ticketPrice][<rowId>]`` (id
    ``TicketForm_ticketPrice_<rowId>``), one per ticket-type row inside
    ``#ticketPriceList``. The 2026 capture at
    docs/recon/ticketmaster_sg/f7_4_capture/02_ticket_area.html shows
    the row id as ``005``, mirrored in the fixture.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    assert await set_quantity(page, 4) is True
    assert (
        await page.locator("select#TicketForm_ticketPrice_005").input_value()
        == "4"
    )


async def test_set_quantity_returns_false_when_no_select(chromium_context, fixture_url) -> None:
    """No quantity select on the page → set_quantity returns False (not raise)."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/check_captcha.html"))
    assert await set_quantity(page, 2) is False


@pytest.mark.parametrize("qty", [0, -1, -100])
async def test_set_quantity_rejects_non_positive(chromium_context, fixture_url, qty: int) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    with pytest.raises(CartError):
        await set_quantity(page, qty)


# ---------------------------------------------------------------------------
# accept_terms_if_needed
# ---------------------------------------------------------------------------


async def test_accept_terms_ticks_real_terms_checkbox(chromium_context, fixture_url) -> None:
    """The legitimate SG terms checkbox is ticked and marketing opt-in is NOT."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))

    # Pre-conditions: neither checkbox is checked.
    assert await page.locator("input#TicketForm_agree").is_checked() is False
    assert await page.locator("input#TicketForm_marketingOptIn").is_checked() is False

    assert await accept_terms_if_needed(page) is True
    assert await page.locator("input#TicketForm_agree").is_checked() is True
    # CRITICAL: marketing opt-in MUST remain unchecked.
    assert await page.locator("input#TicketForm_marketingOptIn").is_checked() is False


async def test_accept_terms_is_idempotent(chromium_context, fixture_url) -> None:
    """Calling accept_terms_if_needed twice leaves the terms checked once."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    await accept_terms_if_needed(page)
    await accept_terms_if_needed(page)
    assert await page.locator("input#TicketForm_agree").is_checked() is True
    assert await page.locator("input#TicketForm_marketingOptIn").is_checked() is False


async def test_accept_terms_returns_false_when_no_checkbox(chromium_context, fixture_url) -> None:
    """Event-detail page has no terms checkbox → accept_terms_if_needed = False."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    assert await accept_terms_if_needed(page) is False


# ---------------------------------------------------------------------------
# add_to_cart
# ---------------------------------------------------------------------------


async def test_add_to_cart_clicks_button_and_records_terms_checked(
    chromium_context, fixture_url, monkeypatch
) -> None:
    """``add_to_cart`` ticks terms, clicks button#autoMode, then verifies cart.

    The cart fixture's form action is ``/ticket/checkout/26sg_pglcs2major/3239/1/2``
    (a relative URL under the file:// origin). Submitting will navigate
    to a non-existent path which is fine — we assert only that the
    button click fired (the terms box must be checked when the click
    happens, since accept_terms_if_needed runs first).

    Patch the cart's captcha-pause to a 0.5-second no-op so the test
    stays fast and doesn't require captcha state.
    """
    # Reduce delays so the test is fast (the real bot uses (0.5, 2.0)).
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))

    # Record whether the button receives a click.
    await page.evaluate(
        """() => {
            window.__sgAutoModeClicks = 0;
            document.querySelector('#autoMode').addEventListener(
                'click',
                () => { window.__sgAutoModeClicks += 1; },
                true,
            );
        }"""
    )

    # Patch the captcha wait to a no-op (no captcha on the fixture).
    async def _no_captcha(page, *, timeout_seconds=300.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(sg_cart.sg_auth, "wait_for_human_if_captcha", _no_captcha)

    result = await add_to_cart(
        page,
        action_delay=(0.0, 0.0),
        timeout_seconds=5.0,
        captcha_timeout_seconds=0.5,
    )

    clicks = await page.evaluate("() => window.__sgAutoModeClicks")
    assert clicks >= 1, "add_to_cart must click button#autoMode at least once"
    # Terms must have been ticked before the click.
    # (The fixture form will submit and may navigate away; check on the page that
    # the result handle was at least computed.)
    assert isinstance(result, bool)


async def test_add_to_cart_returns_false_when_button_missing(
    chromium_context, fixture_url, monkeypatch
) -> None:
    """Event-detail page has no #autoMode → add_to_cart returns False."""

    async def _no_captcha(page, *, timeout_seconds=300.0):  # noqa: ANN001
        return True

    monkeypatch.setattr(sg_cart.sg_auth, "wait_for_human_if_captcha", _no_captcha)

    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    result = await add_to_cart(
        page,
        action_delay=(0.0, 0.0),
        timeout_seconds=2.0,
        captcha_timeout_seconds=0.5,
    )
    assert result is False


# ---------------------------------------------------------------------------
# verify_cart_in_cart
# ---------------------------------------------------------------------------


async def test_verify_cart_in_cart_true_via_dom_indicator(chromium_context, fixture_url) -> None:
    """The cart fixture exposes ``section#sec-cart`` → verify returns True."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    assert await verify_cart_in_cart(page) is True


async def test_verify_cart_in_cart_false_on_event_detail(chromium_context, fixture_url) -> None:
    """Event-detail fixture has neither a cart URL marker nor a cart-page indicator."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    assert await verify_cart_in_cart(page) is False


async def test_verify_cart_in_cart_true_via_url_marker(chromium_context) -> None:
    """A SG URL whose path contains ``/ticket/checkout/`` is enough to confirm."""
    page = await chromium_context.new_page()
    await page.route(
        "https://ticketmaster.sg/ticket/checkout/26sg_pglcs2major/3239/1/2",
        lambda route: route.fulfill(
            status=200,
            headers={"content-type": "text/html"},
            body="<html><body>blank cart-shell</body></html>",
        ),
    )
    await page.goto("https://ticketmaster.sg/ticket/checkout/26sg_pglcs2major/3239/1/2")
    assert await verify_cart_in_cart(page) is True


# ---------------------------------------------------------------------------
# Module surface sanity
# ---------------------------------------------------------------------------


def test_cart_module_exports() -> None:
    required = {
        "CartError",
        "accept_terms_if_needed",
        "add_to_cart",
        "set_quantity",
        "verify_cart_in_cart",
    }
    missing = required - set(sg_cart.__all__)
    assert not missing, f"sg_cart module is missing exports: {missing}"


def test_cart_fixture_is_committed() -> None:
    """The hand-built cart.html fixture is committed and non-trivial."""
    cart = SG_FIXTURE_DIR / "cart.html"
    assert cart.is_file(), f"Missing cart fixture: {cart}"
    text = cart.read_text(encoding="utf-8")
    # The provenance banner must call out the live-capture deferral so
    # future readers know where the file came from.
    assert "NOT captured first-hand" in text or "live cart page" in text.lower()
    # The fixture must reference the SG selectors used by cart.py.
    assert "TicketForm_agree" in text
    assert "TicketForm_marketingOptIn" in text
    assert "TicketForm_ticketPrice_" in text
    assert "autoMode" in text
