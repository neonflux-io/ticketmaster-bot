"""Tests for ``CompositeStrategy`` driven by real headless Chromium.

Each test loads ``tests/fixtures/strategies/composite_basic.html`` (or
``quick_picks_basic.html`` / ``empty_quick_picks.html``) and exercises
the real strategy against the real DOM. Click side-effects are observed
by reading the ``data-click-count`` attribute the fixture's click
recorder maintains.
"""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page

from src.strategies.cheapest import CheapestStrategy
from src.strategies.composite import CompositeStrategy
from src.strategies.multi_section import MultiSectionStrategy
from src.strategies.price_range import PriceRangeStrategy


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


def _rows_html(rows: list[dict[str, str]]) -> str:
    items = []
    for i, row in enumerate(rows):
        attrs = ['data-bdd="quick-pick-row"', f'data-idx="{i}"']
        text = row["text"]
        items.append(f"      <li {' '.join(attrs)}>{text}</li>")
    return '<ul data-bdd="quick-picks-list">\n' + "\n".join(items) + "\n    </ul>"


async def _page_with_rows(context: BrowserContext, rows: list[dict[str, str]]) -> Page:
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


# --- fixture-backed tests --------------------------------------------------


async def test_composite_returns_first_non_none_child(chromium_context, fixture_url):
    """[dom.composite-happy]: with two children that would both pick the
    same row in the fixture, the composite returns the first child's
    candidate and that row is clicked exactly once.

    Fixture invariants (see composite_basic.html):
      - PriceRangeStrategy(0, 200) cheapest = idx=0 ($150, section 100)
      - MultiSectionStrategy(["100"]) cheapest = idx=0 ($150)
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("composite_basic"))

    strategy = CompositeStrategy(
        children=[
            PriceRangeStrategy(min_price=0.0, max_price=200.0),
            MultiSectionStrategy(sections=["100"]),
        ]
    )
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.price == 150.0
    assert chosen.section == "100"
    assert await _click_count(page, 0) == 1
    for other in (1, 2, 3, 4):
        assert await _click_count(page, other) == 0


async def test_composite_returns_none_when_no_child_matches(chromium_context, fixture_url):
    """[dom.composite-reject]: when every child returns None, composite
    returns None and no row records a click."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("composite_basic"))

    # Both children's candidate sets are empty against this fixture:
    #   - PriceRangeStrategy(500, 1000): every row is under $500.
    #   - MultiSectionStrategy(["999"]): no section-999 rows.
    strategy = CompositeStrategy(
        children=[
            PriceRangeStrategy(min_price=500.0, max_price=1000.0),
            MultiSectionStrategy(sections=["999"]),
        ]
    )
    assert await strategy.pick(page) is None
    for idx in range(5):
        assert await _click_count(page, idx) == 0


async def test_composite_empty_quick_picks_returns_none(chromium_context, fixture_url):
    """[dom.empty-candidates-none]: empty fixture ⇒ composite returns None."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("empty_quick_picks"))

    strategy = CompositeStrategy(
        children=[
            CheapestStrategy(),
            MultiSectionStrategy(sections=["100"]),
        ]
    )
    assert await strategy.pick(page) is None


# --- inline-rendered behavior tests ---------------------------------------


async def test_composite_first_child_misses_second_child_wins(chromium_context):
    """When the first child returns None but the second matches, the
    composite returns the second child's candidate and clicks it once."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 200 Row A — $80.00"},
            {"text": "Section 200 Row B — $120.00"},
            {"text": "Section 100 Row C — $300.00"},
        ],
    )
    # PriceRangeStrategy(0, 50): every row is over $50 ⇒ None.
    # MultiSectionStrategy(["100"]): only idx=2 matches ⇒ $300 row.
    strategy = CompositeStrategy(
        children=[
            PriceRangeStrategy(min_price=0.0, max_price=50.0),
            MultiSectionStrategy(sections=["100"]),
        ]
    )
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.price == 300.0
    assert await _click_count(page, 2) == 1
    for other in (0, 1):
        assert await _click_count(page, other) == 0


async def test_composite_short_circuits_after_first_success(chromium_context):
    """Once a child returns a candidate (and clicks it), later children
    are not consulted — only one row is ever clicked.

    We verify by giving the first child a known-good candidate and the
    second child a totally different match; the first child's row must
    be the only one clicked.
    """
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — $50.00"},
            {"text": "Section 200 Row B — $400.00"},
        ],
    )
    # PriceRangeStrategy(0, 100) picks idx=0 ($50).
    # MultiSectionStrategy(["200"]) would pick idx=1, but should never run.
    strategy = CompositeStrategy(
        children=[
            PriceRangeStrategy(min_price=0.0, max_price=100.0),
            MultiSectionStrategy(sections=["200"]),
        ]
    )
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 50.0
    assert await _click_count(page, 0) == 1
    assert await _click_count(page, 1) == 0


def test_composite_empty_children_raises():
    """A composite constructed with an empty children list raises so the
    misconfiguration is surfaced loudly instead of silently no-op'ing."""
    import pytest

    with pytest.raises(ValueError):
        CompositeStrategy(children=[])


async def test_composite_single_child_acts_as_passthrough(chromium_context):
    """With one child, the composite returns exactly what that child returns."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — $200.00"},
            {"text": "Section 200 Row B — $90.00"},
        ],
    )
    strategy = CompositeStrategy(children=[CheapestStrategy()])
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 90.0
    assert await _click_count(page, 1) == 1
