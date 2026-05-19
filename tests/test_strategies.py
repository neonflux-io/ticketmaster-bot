"""Tests for ticket-selection strategy modules driven by real headless Chromium.

Each DOM-touching test loads either an HTML file from ``tests/fixtures/`` via
the shared ``fixture_url`` helper or sets the page content inline with the
same ``[data-bdd=...]`` attributes that the selector registry expects. The
suite runs against a real Playwright browser; no test doubles remain.
"""

from __future__ import annotations

import pytest
from playwright.async_api import BrowserContext, Page

from src.strategies.base import (
    SelectionStrategy,
    TicketCandidate,
    _extract_price,
    _extract_row,
    _extract_sections,
)
from src.strategies.best_available import BestAvailableStrategy
from src.strategies.cheapest import CheapestStrategy
from src.strategies.section_target import SectionTargetStrategy
from src.utils.config_loader import SectionTargetConfig

# --- regex helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Section 108  Row C  $89.50", 89.50),
        ("$1,234.56 each", 1234.56),
        ("Total $1,234", 1234.0),
        ("$99 each", 99.0),
        ("no price here", None),
    ],
)
def test_extract_price(text, expected):
    assert _extract_price(text) == expected


def test_extract_sections_multiple():
    assert _extract_sections("Section 108 - Sec 109") == ["108", "109"]


def test_extract_sections_single_uppercased():
    assert _extract_sections("sec a12") == ["A12"]


def test_extract_row():
    assert _extract_row("Section 100 Row B") == "B"
    assert _extract_row("Sec 100") is None


# --- helpers --------------------------------------------------------------


def _rows_html(rows: list[dict[str, str]]) -> str:
    """Build a ``<ul data-bdd='quick-picks-list'>`` block from row specs.

    Each ``rows`` entry may carry the keys ``text`` and (optionally)
    ``price_level_id`` plus an ``idx`` to make click-count assertions
    unambiguous.
    """
    items = []
    for i, row in enumerate(rows):
        attrs = ['data-bdd="quick-pick-row"', f'data-idx="{i}"']
        if row.get("price_level_id"):
            attrs.append(f'data-price-level-id="{row["price_level_id"]}"')
        text = row["text"]
        items.append(f"      <li {' '.join(attrs)}>{text}</li>")
    return '<ul data-bdd="quick-picks-list">\n' + "\n".join(items) + "\n    </ul>"


def _click_recorder_script() -> str:
    return (
        "window.__clicks = [];"
        "document.addEventListener('click', function (event) {"
        "  var row = event.target.closest('[data-bdd=\"quick-pick-row\"]');"
        "  if (!row) return;"
        "  window.__clicks.push(row.getAttribute('data-idx'));"
        "  var prev = parseInt(row.getAttribute('data-click-count') || '0', 10);"
        "  row.setAttribute('data-click-count', String(prev + 1));"
        "}, true);"
    )


async def _page_with_rows(context: BrowserContext, rows: list[dict[str, str]]) -> Page:
    """Open a new page whose body contains a quick-picks list of ``rows``."""
    html = (
        '<!doctype html><html><body><main role="main">'
        f"{_rows_html(rows)}"
        "</main>"
        f"<script>{_click_recorder_script()}</script>"
        "</body></html>"
    )
    page = await context.new_page()
    await page.set_content(html)
    return page


async def _click_count(page: Page, idx: int) -> int:
    return int(
        await page.locator(f'[data-bdd="quick-pick-row"][data-idx="{idx}"]').get_attribute(
            "data-click-count"
        )
        or "0"
    )


# --- CheapestStrategy ----------------------------------------------------


async def test_cheapest_picks_lowest_under_max_price(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — $200.00"},
            {"text": "Section 200 Row C — $100.00"},
            {"text": "Section 300 Row D — $1,500.00"},
        ],
    )
    strategy = CheapestStrategy(max_price=300.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 100.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0
    assert await _click_count(page, 2) == 0


async def test_cheapest_rejects_when_over_max_price(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [{"text": "Section 100 $500.00"}],
    )
    strategy = CheapestStrategy(max_price=100.0)
    assert await strategy.pick(page) is None
    assert await _click_count(page, 0) == 0


async def test_cheapest_handles_four_digit_price(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 1 $999.00"},
            {"text": "Section 2 $1,200.00"},
        ],
    )
    strategy = CheapestStrategy(max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 999.0


# --- BestAvailableStrategy -----------------------------------------------


_BEST_AVAILABLE_PAGE_TEMPLATE = (
    '<!doctype html><html><body><main role="main">'
    '<button data-bdd="best-available">Best Available</button>'
    '<section data-bdd="order-summary">{summary}</section>'
    "{extra}"
    "</main></body></html>"
)


async def test_best_available_uses_dedicated_button_with_price_in_summary(
    chromium_context,
):
    page = await chromium_context.new_page()
    await page.set_content(
        _BEST_AVAILABLE_PAGE_TEMPLATE.format(summary="Subtotal $250.00", extra="")
    )
    strategy = BestAvailableStrategy(max_price=500.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 250.0


async def test_best_available_aborts_when_over_max_price(chromium_context):
    page = await chromium_context.new_page()
    await page.set_content(
        _BEST_AVAILABLE_PAGE_TEMPLATE.format(summary="Subtotal $999.00", extra="")
    )
    strategy = BestAvailableStrategy(max_price=200.0)
    assert await strategy.pick(page) is None


async def test_best_available_aborts_when_price_unknown_and_max_set(chromium_context):
    page = await chromium_context.new_page()
    # No order-summary element rendered, so price cannot be verified.
    await page.set_content(
        '<!doctype html><html><body><main role="main">'
        '<button data-bdd="best-available">Best Available</button>'
        "</main></body></html>"
    )
    strategy = BestAvailableStrategy(max_price=100.0)
    assert await strategy.pick(page) is None


async def test_best_available_falls_back_to_first_row(chromium_context):
    page = await chromium_context.new_page()
    rows_block = _rows_html(
        [
            {"text": "Section 1 $50.00"},
            {"text": "Section 2 $60.00"},
        ]
    )
    await page.set_content(
        '<!doctype html><html><body><main role="main">'
        f"{rows_block}"
        "</main>"
        f"<script>{_click_recorder_script()}</script>"
        "</body></html>"
    )
    strategy = BestAvailableStrategy(max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 50.0
    assert await _click_count(page, 0) == 1


# --- SectionTargetStrategy ------------------------------------------------


async def test_section_target_matches_section_and_row(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row Z $80.00"},
            {"text": "Section 100 Row B $90.00"},
            {"text": "Section 200 Row A $40.00"},
        ],
    )
    target = SectionTargetConfig(section="100", row_range=["A", "M"], price_level_id=None)
    strategy = SectionTargetStrategy(target, max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.row == "B"


async def test_section_target_filters_by_price_level_id(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 $80.00", "price_level_id": "PL1"},
            {"text": "Section 100 $90.00", "price_level_id": "PL2"},
        ],
    )
    target = SectionTargetConfig(section=None, row_range=None, price_level_id="PL2")
    strategy = SectionTargetStrategy(target, max_price=None)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 90.0


async def test_section_target_no_match_returns_none(chromium_context):
    page = await _page_with_rows(chromium_context, [{"text": "Section 100 $80.00"}])
    target = SectionTargetConfig(section="999", row_range=None, price_level_id=None)
    assert await SectionTargetStrategy(target, max_price=None).pick(page) is None


# --- click retry ----------------------------------------------------------


async def test_click_candidate_retries_on_failure(chromium_context):
    """The candidate carries a locator that resolves to nothing (so the first
    click attempt fails fast); ``click_candidate`` then re-resolves by
    description match and succeeds on the second attempt against the real
    row in the freshly-listed set.
    """
    page = await _page_with_rows(chromium_context, [{"text": "Section 100 Row A $50.00"}])
    strategy = CheapestStrategy(max_price=None)
    candidates = await strategy.list_candidates(page)
    assert len(candidates) == 1
    candidate = candidates[0]

    # Replace the candidate's locator with one whose CSS selector matches no
    # element. ``scroll_into_view_if_needed`` will raise with a short
    # ``state="attached"`` wait (achieved by Playwright's strict mode rejecting
    # the empty match almost immediately when the parent already exists).
    candidate.locator = page.locator("li.no-such-class-xy7zzz-not-present")

    clicked = await strategy.click_candidate(candidate, page, attempts=2)
    assert clicked is True
    # The real row received exactly one click via the retry path.
    assert await _click_count(page, 0) == 1


# --- abstract surface check (ensures interface stays stable) -------------


def test_selection_strategy_is_abstract():
    with pytest.raises(TypeError):
        SelectionStrategy()  # type: ignore[abstract]


async def test_ticket_candidate_repr_ok(chromium_context):
    page = await chromium_context.new_page()
    await page.set_content(
        '<!doctype html><html><body><div data-bdd="quick-pick-row">x</div></body></html>'
    )
    loc = page.locator("[data-bdd='quick-pick-row']").first
    tc = TicketCandidate(
        locator=loc,
        price=50.0,
        section="100",
        row="A",
        description="test",
    )
    assert "price=50.0" in repr(tc)
