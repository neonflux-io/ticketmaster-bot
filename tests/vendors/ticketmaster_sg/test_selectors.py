"""Regression test: every SG ticket-area selector resolves on the live 2026 DOM.

This file is the canary that catches a recurrence of the F7.4 capture
failure where ``ticket_area_quantity_select`` was hard-coded to
``select#TicketForm_count`` — an id that does NOT exist on the live
ticketmaster.sg "Choose Your Tickets" page. The actual quantity
``<select>`` lives inside the AJAX-injected ``#priceList`` form as
``TicketForm[ticketPrice][<rowId>]``.

The test drives a raw copy of the live DOM captured under
``docs/recon/ticketmaster_sg/f7_4_capture/02_ticket_area.html`` (231 KB,
captured 2026-05-20 against
https://ticketmaster.sg/ticket/area/26sg_pglcs2major/3239) under real
headless Chromium and asserts that every ticket-area logical name in
``config/selectors/ticketmaster_sg.yaml`` resolves to at least one DOM
element. If the live site renames an attribute or moves the form into
an iframe, the YAML must grow a new fallback or this test fails until
the registry is updated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.vendors.ticketmaster_sg import selectors as sg_selectors

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_CAPTURE = (
    REPO_ROOT
    / "docs"
    / "recon"
    / "ticketmaster_sg"
    / "f7_4_capture"
    / "02_ticket_area.html"
)


# Logical names that the SG vendor adapter uses on the ticket-area page.
# Every name listed here must resolve against the live capture; the
# registry's combined ``", "``-joined fallback chain is exercised via
# ``sg_selectors.locator``.
_TICKET_AREA_LOGICAL_NAMES = (
    "ticket_area_event_select",
    "ticket_area_quantity_select",
    "ticket_area_best_available_button",
    "ticket_area_map_container",
    "ticket_type_row",
    "ticket_type_price_text",
    "ticket_type_name_text",
    "ticket_type_quantity_select",
    "ticket_type_price_size_input",
    "ticket_form_submit_button",
    "csrf_input",
    "cart_quantity_select",
    "cart_add_to_cart_button",
)


def test_live_capture_exists() -> None:
    """The 2026 capture used as the regression source must be present.

    If this asserts, the f7_4_capture corpus was deleted; re-run
    ``docs/recon/ticketmaster_sg/_cart_capture.py`` (or copy from a known
    good archive) so the regression below has its source-of-truth back.
    """
    assert LIVE_CAPTURE.is_file(), (
        f"Live ticket-area capture missing: {LIVE_CAPTURE}. "
        "Re-run docs/recon/ticketmaster_sg/_cart_capture.py to refresh."
    )
    # >50 KB guards against an empty / placeholder file.
    assert LIVE_CAPTURE.stat().st_size > 50_000, (
        f"Live ticket-area capture is suspiciously small: "
        f"{LIVE_CAPTURE.stat().st_size} bytes"
    )


@pytest.mark.parametrize("logical_name", _TICKET_AREA_LOGICAL_NAMES)
async def test_ticket_area_selector_resolves_on_live_capture(
    chromium_context,  # noqa: ANN001 - pytest fixture
    logical_name: str,
) -> None:
    """Each SG ticket-area selector matches ≥1 element on the live capture.

    Uses the SG selector registry (the same code path the adapter takes
    in production) so a regression in either the YAML or the registry
    helper trips this test.
    """
    page = await chromium_context.new_page()
    try:
        await page.goto(LIVE_CAPTURE.as_uri())
        combined = sg_selectors.selector_for(logical_name)
        count = await page.locator(combined).count()
        assert count >= 1, (
            f"SG selector {logical_name!r} ({combined!r}) matched 0 elements "
            f"on the live ticket-area capture at {LIVE_CAPTURE.relative_to(REPO_ROOT)}"
        )
    finally:
        await page.close()


async def test_ticket_area_quantity_select_actually_drives_a_value(
    chromium_context,  # noqa: ANN001 - pytest fixture
) -> None:
    """``ticket_area_quantity_select`` is interactive, not just a passive match.

    Captures the F7.4 regression at full fidelity: the bot fails not
    when the selector is missing entirely but when it matches a
    non-functional element. Exercising ``select_option`` here proves
    Playwright can drive the real quantity ``<select>`` element.
    """
    page = await chromium_context.new_page()
    await page.goto(LIVE_CAPTURE.as_uri())
    qty = sg_selectors.locator(page, "ticket_area_quantity_select")
    await qty.wait_for(state="attached", timeout=5_000)
    await qty.select_option(value="2")
    assert await qty.input_value() == "2"


def test_ticket_area_quantity_select_yaml_lists_the_form_name_first() -> None:
    """The first fallback for ``ticket_area_quantity_select`` is the form-name
    pattern, not the row-specific id.

    Form names are far more stable than per-row ids on the SG Yii stack,
    so the registry's first try should be the broadest, most stable
    selector. This test pins that ordering so a future edit that puts a
    fragile id back at the top trips immediately.
    """
    fallbacks = sg_selectors.get("ticket_area_quantity_select")
    assert fallbacks, "ticket_area_quantity_select must have at least one fallback"
    assert "TicketForm[ticketPrice]" in fallbacks[0], (
        f"First fallback should match the form-name pattern; got {fallbacks[0]!r}"
    )
    # The defunct ``TicketForm_count`` id must NOT be in the chain.
    assert not any("TicketForm_count" in fb for fb in fallbacks), (
        f"Fallback chain still references the defunct TicketForm_count id: "
        f"{fallbacks!r}"
    )
