"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.checkout`.

Drives the live ``cart.html`` / ``checkout.html`` fixtures (F7.6
capture of the combined SG ``/ticket/checkout`` page). The live SG
DOM is structurally distinct from the F7.4 hand-built draft: delivery
options are exposed as ``<option>``s inside
``select#checkoutform-shipmentid`` (currently "Courier" + "Mobile
Ticket"; other SG venues add more), payment is a single bundled
``input#checkoutform-paymentid-88`` radio that covers every accepted
method (Visa / MC / AMEX / Atome / GrabPay / Apple Pay / Google Pay
/ WeChat), and the Place-Order trigger is ``button#submitButton``
inside ``form#form-ticket-checkout``. The fixtures have been
PII-scrubbed (see provenance banner inside each file) before commit.
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


_DELIVERY_SELECT = "select#checkoutform-shipmentid"


async def test_select_delivery_picks_mobile_when_preferred(chromium_context, fixture_url) -> None:
    """``mobile ticket`` preference picks option value=10 (Mobile Ticket).

    The live SG ``/ticket/checkout`` DOM exposes delivery as a native
    ``<select>`` (id ``checkoutform-shipmentid``). ``Mobile Ticket`` is
    option value=10; ``Courier`` is option value=3. The default SG
    preference list (``SG_DEFAULT_DELIVERY_PREFERENCE``) lists both
    "mobile entry" and "mobile ticket" as substring matches, so either
    keyword resolves to the Mobile Ticket option.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["mobile ticket"], allow_any=False)
    assert result is True
    assert await page.locator(_DELIVERY_SELECT).input_value() == "10"


async def test_select_delivery_respects_preference_order(chromium_context, fixture_url) -> None:
    """When a higher-priority keyword matches, the lower-priority one is skipped."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    # Preference list: Courier first (matches "Courier" option, value=3),
    # then Mobile Ticket. Courier should win.
    result = await select_delivery(page, preferred=["courier", "mobile ticket"], allow_any=False)
    assert result is True
    assert await page.locator(_DELIVERY_SELECT).input_value() == "3"


async def test_select_delivery_courier(chromium_context, fixture_url) -> None:
    """Explicit ``courier`` keyword matches option value=3."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["courier"], allow_any=False)
    assert result is True
    assert await page.locator(_DELIVERY_SELECT).input_value() == "3"


async def test_select_delivery_no_match_no_allow_any(chromium_context, fixture_url) -> None:
    """No preferred keyword matches and allow_any=False → returns False, picks nothing.

    The select's pre-render value is the empty "Please Select"
    placeholder; the helper must leave it untouched.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["telepathy"], allow_any=False)
    assert result is False
    # The select stays on the "Please Select" placeholder (value="").
    assert await page.locator(_DELIVERY_SELECT).input_value() == ""


async def test_select_delivery_allow_any_fallback(chromium_context, fixture_url) -> None:
    """No preferred match but allow_any=True → first non-placeholder option picked.

    On the live SG capture the first non-placeholder ``<option>`` is
    "Courier" (value=3); the helper picks that as the last-resort
    fallback.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await select_delivery(page, preferred=["xyz"], allow_any=True)
    assert result is True
    assert await page.locator(_DELIVERY_SELECT).input_value() == "3"


# ---------------------------------------------------------------------------
# select_saved_card
# ---------------------------------------------------------------------------


# Saved-card selection is driven by ``checkout_saved_cards.html``: the
# live SG ``/ticket/checkout`` capture only renders one bundled payment
# radio (Visa/MC/AMEX/Atome/GrabPay/Apple Pay/Google Pay/WeChat) and
# never exposes saved cards, so this dedicated fixture is the only way
# to gate the matcher templates documented in
# ``config/selectors/ticketmaster_sg.yaml`` (``ending in {last_four}``
# and ``**** {last_four}``). See the fixture banner for provenance.
_SAVED_CARDS_FIXTURE = "vendors/ticketmaster_sg/checkout_saved_cards.html"


async def test_select_saved_card_matches_visa_ending_in(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url(_SAVED_CARDS_FIXTURE))
    assert await select_saved_card(page, "4242") is True
    assert await page.locator("input#payment_saved_4242").is_checked() is True


async def test_select_saved_card_matches_mc_asterisk_format(chromium_context, fixture_url) -> None:
    """The Mastercard label uses '**** 1111' format — matcher template covers it."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url(_SAVED_CARDS_FIXTURE))
    assert await select_saved_card(page, "1111") is True
    assert await page.locator("input#payment_saved_1111").is_checked() is True


async def test_select_saved_card_none_input(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url(_SAVED_CARDS_FIXTURE))
    assert await select_saved_card(page, None) is False
    assert await page.locator("input#payment_saved_4242").is_checked() is False


@pytest.mark.parametrize("bad_last_four", ["", "12", "12345", "abcd"])
async def test_select_saved_card_rejects_bad_last_four(
    chromium_context, fixture_url, bad_last_four: str
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url(_SAVED_CARDS_FIXTURE))
    assert await select_saved_card(page, bad_last_four) is False


async def test_select_saved_card_returns_false_when_card_not_present(
    chromium_context, fixture_url
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url(_SAVED_CARDS_FIXTURE))
    assert await select_saved_card(page, "9999") is False


async def test_select_saved_card_returns_false_on_live_bundled_payment_page(
    chromium_context, fixture_url
) -> None:
    """The live SG ``/ticket/checkout`` page exposes no saved cards.

    The captured account has no saved cards on file so the live DOM
    only renders one bundled payment radio. The helper must report
    that no saved-card last-four matched (False) rather than
    accidentally ticking the bundled payment radio.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    assert await select_saved_card(page, "4242") is False


# ---------------------------------------------------------------------------
# verify_cart_matches
# ---------------------------------------------------------------------------


# The live SG cart.html capture is the combined ``/ticket/checkout``
# page from a 4-ticket order at $144 base price per ticket
# (subtotal $150 incl. $6 booking fee per ticket, total $600). The
# section label is ``GEN ADM`` (the multi-word form that exercises
# the ``_extract_sg_sections`` / ``_normalise_section`` SG-aware
# matchers).
_LIVE_QTY = 4
_LIVE_BASE_PRICE = 144.00
_LIVE_SECTION = "GENADM"


async def test_verify_cart_matches_happy_path(chromium_context, fixture_url) -> None:
    """Live cart fixture matches a candidate with section=GENADM, $144 base, qty=4.

    The SG-specific extractor (``_extract_sg_sections`` in the
    checkout module) handles both the compact ``GENADM`` and the
    space-separated ``GEN ADM`` forms via the
    :func:`_normalise_section` helper. The shared US extractor in
    ``src.strategies.base._extract_sections`` would only capture the
    first token from ``Section: GEN ADM``; F7.6 widens the SG-side
    comparison so the live order summary's multi-word labels match
    the compact candidate form too.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    candidate = _FakeCandidate(
        section=_LIVE_SECTION, price=_LIVE_BASE_PRICE, description="Standard"
    )
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=_LIVE_QTY,
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
            expected_quantity=_LIVE_QTY,
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
            expected_quantity=99,  # cart fixture shows 4 tickets
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
            expected_quantity=_LIVE_QTY,
            expected_candidate=candidate,
            price_tolerance=0.05,
        )
        is False
    )


async def test_verify_cart_matches_section_mismatch(chromium_context, fixture_url) -> None:
    """Candidate section different from cart section → False."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    candidate = _FakeCandidate(section="FLOOR", price=_LIVE_BASE_PRICE)
    assert (
        await verify_cart_matches(
            page,
            expected_quantity=_LIVE_QTY,
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


_PLACE_ORDER_BUTTON = "button#submitButton"


async def test_run_checkout_halts_at_review_when_auto_purchase_false(
    chromium_context, fixture_url
) -> None:
    """auto_purchase=False must stop BEFORE clicking the SG Place-Order trigger.

    On the live SG ``/ticket/checkout`` capture the final submit
    button is ``button#submitButton`` ("Checkout" text) inside
    ``form#form-ticket-checkout``. ``card_last_four`` is None because
    the live page has no saved cards (see the dedicated
    ``test_select_saved_card_*`` cases).
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    # Record clicks on the SG submit button.
    await page.evaluate(
        """() => {
            window.__sgPlaceOrderClicks = 0;
            document.querySelector('#submitButton').addEventListener(
                'click',
                () => { window.__sgPlaceOrderClicks += 1; },
                true,
            );
        }"""
    )

    result = await run_checkout(
        page,
        auto_purchase=False,
        card_last_four=None,
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile ticket"],
    )

    assert result is True
    # Delivery was picked (Mobile Ticket = value 10), but the Place
    # Order trigger MUST NOT have been clicked.
    assert await page.locator(_DELIVERY_SELECT).input_value() == "10"
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0, "auto_purchase=False must NEVER click the Place-Order trigger"


async def test_run_checkout_uses_default_delivery_preference(chromium_context, fixture_url) -> None:
    """When preferred_delivery=None, the SG defaults pick Mobile Ticket.

    ``SG_DEFAULT_DELIVERY_PREFERENCE`` lists "mobile entry" / "mobile
    ticket" / "e-ticket" etc. in order; the live capture exposes
    "Mobile Ticket" (value=10) and "Courier" (value=3), so the
    default preference resolves to Mobile Ticket.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    result = await run_checkout(
        page,
        auto_purchase=False,
        card_last_four=None,
        action_delay=(0.0, 0.0),
    )
    assert result is True
    assert await page.locator(_DELIVERY_SELECT).input_value() == "10"


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
            document.querySelector('#submitButton').addEventListener(
                'click',
                () => { window.__sgPlaceOrderClicks += 1; },
                true,
            );
        }"""
    )

    result = await run_checkout(
        page,
        auto_purchase=True,
        card_last_four=None,
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile ticket"],
        expected_quantity=99,
        expected_candidate=None,
        captcha_timeout_seconds=0.5,
    )

    assert result is False
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0, "Cart mismatch must abort BEFORE clicking the Place-Order trigger"


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
    """The live checkout.html capture is committed, PII-scrubbed, complete.

    Asserts on structure that is stable across SG users + sessions
    (page-title, form id, delivery select id, the bundled-payment
    radio id, the SG submit button id, and the SG-specific payment
    method names listed on the page) — never on user-specific PII
    (which has been scrubbed before commit).
    """
    fx = SG_FIXTURE_DIR / "checkout.html"
    assert fx.is_file()
    text = fx.read_text(encoding="utf-8")
    # Provenance banner inserted by the F7.6b scrubber.
    assert "PROVENANCE: live SG" in text
    assert "SCRUBBED for commit" in text
    # PII / session secrets must NOT have leaked into the commit.
    assert "ziqibrandonli" not in text.lower()
    assert "+6580222544" not in text
    assert "Ziqi Li" not in text
    # SG-specific payment methods listed in the bundled-payment block.
    assert "GrabPay" in text
    assert "Apple Pay" in text
    # SG delivery options exposed via the native ``<select>``.
    assert "Mobile Ticket" in text
    assert "Courier" in text
    # SG submit button and form (Place-Order trigger lives here).
    assert 'id="form-ticket-checkout"' in text
    assert 'id="submitButton"' in text
    # The bundled-payment radio that covers Visa/MC/AMEX/Atome/etc.
    assert "checkoutform-paymentid-88" in text
    # The native delivery select.
    assert "checkoutform-shipmentid" in text
    # SGD price markers from the live order summary.
    assert "$144.00" in text
    assert "$600.00" in text


def test_saved_cards_fixture_is_committed() -> None:
    """The hand-built saved-cards fixture mirrors the YAML template shapes."""
    fx = SG_FIXTURE_DIR / "checkout_saved_cards.html"
    assert fx.is_file()
    text = fx.read_text(encoding="utf-8")
    assert "ending in 4242" in text
    assert "**** 1111" in text
    assert "payment_saved_4242" in text
    assert "payment_saved_1111" in text


def test_checkout_error_is_exception() -> None:
    assert issubclass(CheckoutError, Exception)


# ---------------------------------------------------------------------------
# Section extraction + normalisation (F7.6: SG multi-word section labels)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Section: GENADM", ["GENADM"]),
        ("Section: GEN ADM", ["GEN ADM"]),
        ("Section GENADM", ["GENADM"]),
        ("Section GENERAL ADMISSION", ["GENERAL ADMISSION"]),
        ("Section 108 - Sec 109", ["108", "109"]),
        # The stop-words list trims trailing summary-row tokens so the
        # extractor never spills into "Quantity 2 tickets" when the SG
        # inner_text concatenates two table cells.
        ("Section GENADM Quantity 2 tickets", ["GENADM"]),
        ("Section: GEN ADM Subtotal SGD 288.00", ["GEN ADM"]),
    ],
)
def test_extract_sg_sections_handles_sg_label_shapes(
    text: str, expected: list[str]
) -> None:
    from src.vendors.ticketmaster_sg.checkout import _extract_sg_sections

    assert _extract_sg_sections(text) == expected


@pytest.mark.parametrize(
    "a, b",
    [
        ("GENADM", "GEN ADM"),
        ("GEN ADM", "genadm"),
        ("GENERAL ADMISSION", "general admission"),
        (" GEN ADM ", "GENADM"),
    ],
)
def test_normalise_section_treats_spaces_as_no_op(a: str, b: str) -> None:
    from src.vendors.ticketmaster_sg.checkout import _normalise_section

    assert _normalise_section(a) == _normalise_section(b)
