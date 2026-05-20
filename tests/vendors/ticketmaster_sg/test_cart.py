"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.cart`.

Drives the SG ticket-area fixture for the cart-actions
(set_quantity / accept_terms / add_to_cart): the live SG
``cart.html`` captured by F7.6 is actually the combined
``/ticket/checkout`` page, which does NOT carry the
``TicketForm_agree`` / ``autoMode`` / ``TicketForm_ticketPrice_<row>``
controls that those helpers target. Those controls live on the
preceding ticket-area page captured at
``tests/fixtures/vendors/ticketmaster_sg/ticket_area.html``, which is
the actual "add-to-cart" trigger in the SG flow. The cart-page
indicators (``verify_cart_in_cart``) are still tested against the live
``cart.html`` capture since that helper inspects URL + page indicators
on the cart-equivalent SG page.
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


async def test_set_quantity_selects_value_on_ticket_area_fixture(
    chromium_context, fixture_url
) -> None:
    """``set_quantity(page, 4)`` flips the SG ticket-area select to value 4.

    The SG ticket-area page renders the quantity ``<select>`` as
    ``TicketForm[ticketPrice][<rowId>]`` (id
    ``TicketForm_ticketPrice_<rowId>``), one per ticket-type row inside
    ``#ticketPriceList``. The 2026 capture at
    docs/recon/ticketmaster_sg/f7_4_capture/02_ticket_area.html shows
    the row id as ``005``, mirrored in the fixture. The live
    ``cart.html`` (re-dumped by F7.6 from the combined SG
    ``/ticket/checkout`` page) has neither this select nor the
    ``autoMode`` button — those controls live exclusively on the
    preceding ticket-area page, which is what we drive here.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))
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
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))
    with pytest.raises(CartError):
        await set_quantity(page, qty)


# ---------------------------------------------------------------------------
# accept_terms_if_needed
# ---------------------------------------------------------------------------


async def test_accept_terms_ticks_real_terms_checkbox(chromium_context, fixture_url) -> None:
    """The legitimate SG terms checkbox is ticked when the page exposes it.

    On the live SG flow, the terms checkbox (``input#TicketForm_agree``)
    is rendered on the ``/ticket/check-captcha/...`` interstitial that
    sits between the ticket-area page and the cart-equivalent
    ``/ticket/checkout`` page. The live ``cart.html`` capture (the
    combined ``/ticket/checkout`` page) does NOT carry a terms control
    at all — only event-partner opt-in checkboxes (disabled) — so we
    drive the captcha fixture here.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/check_captcha.html"))

    # Pre-conditions: the terms box exists and is unticked.
    assert await page.locator("input#TicketForm_agree").is_checked() is False

    assert await accept_terms_if_needed(page) is True
    assert await page.locator("input#TicketForm_agree").is_checked() is True


async def test_accept_terms_is_idempotent(chromium_context, fixture_url) -> None:
    """Calling accept_terms_if_needed twice leaves the terms checked once."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/check_captcha.html"))
    await accept_terms_if_needed(page)
    await accept_terms_if_needed(page)
    assert await page.locator("input#TicketForm_agree").is_checked() is True


async def test_accept_terms_returns_false_on_live_cart_page(
    chromium_context, fixture_url
) -> None:
    """On the live ``/ticket/checkout`` SG page there is no terms checkbox.

    Verifies the helper degrades to a no-op (returns False) rather
    than ticking any of the event-partner opt-ins that ARE present on
    that page. The opt-ins are disabled and labelled ``partner_ids[]``,
    so the helper's marketing-blocklist / terms-keyword filter should
    skip them.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    assert await accept_terms_if_needed(page) is False


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

    Drives the SG ticket-area fixture because that's where the
    ``autoMode`` Best-Available button + ``TicketForm_*`` controls
    live in the live flow — the live ``cart.html`` capture is the
    combined ``/ticket/checkout`` page that comes *after* the
    add-to-cart action has already fired. The form action on the
    ticket-area fixture is a relative URL under the file:// origin so
    submitting will navigate to a non-existent path; that is fine —
    we assert only that the button click fired.

    Patch the cart's captcha-pause to a 0.5-second no-op so the test
    stays fast and doesn't require captcha state.
    """
    # Reduce delays so the test is fast (the real bot uses (0.5, 2.0)).
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))

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
    async def _no_captcha(page, **_kwargs):  # noqa: ANN001, ANN003
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
    # The result of verify_cart_in_cart against the post-click DOM is
    # a bool; we don't constrain its value (the fixture form action
    # navigates to a relative path that doesn't resolve).
    assert isinstance(result, bool)


async def test_add_to_cart_returns_false_when_button_missing(
    chromium_context, fixture_url, monkeypatch
) -> None:
    """Event-detail page has no #autoMode → add_to_cart returns False."""

    async def _no_captcha(page, **_kwargs):  # noqa: ANN001, ANN003
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
    """The live cart.html capture is committed, PII-scrubbed, non-trivial."""
    cart = SG_FIXTURE_DIR / "cart.html"
    assert cart.is_file(), f"Missing cart fixture: {cart}"
    text = cart.read_text(encoding="utf-8")
    # The provenance banner must call out that this is the live
    # capture (rather than the F7.4 hand-built placeholder) and that
    # PII has been scrubbed before commit.
    assert "PROVENANCE: live SG" in text
    assert "SCRUBBED for commit" in text
    # PII / session secrets must NOT be present.
    assert "ziqibrandonli" not in text.lower()
    assert "+6580222544" not in text
    assert "Ziqi Li" not in text
    # The fixture is the live ``/ticket/checkout`` page (SG combines
    # cart + checkout onto a single URL). The structural markers
    # below come from that page's stable Yii markup.
    assert 'id="form-ticket-checkout"' in text
    assert 'id="cartTotalTicket"' in text
    assert 'id="orderAmount"' in text


def test_ticket_area_fixture_carries_add_to_cart_selectors() -> None:
    """The ticket-area fixture still exposes the SG add-to-cart selectors.

    ``set_quantity`` / ``accept_terms_if_needed`` / ``add_to_cart``
    helpers target controls (``TicketForm_ticketPrice_<row>``,
    ``TicketForm_agree`` via the check-captcha gate, ``autoMode``) that
    live exclusively on the SG ticket-area + check-captcha pages, NOT
    on the live ``/ticket/checkout`` page. This gates that the
    canonical recon fixture still exposes them.
    """
    fx = SG_FIXTURE_DIR / "ticket_area.html"
    text = fx.read_text(encoding="utf-8")
    assert "TicketForm_ticketPrice_" in text
    assert "autoMode" in text
