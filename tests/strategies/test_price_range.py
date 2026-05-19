"""Tests for ``PriceRangeStrategy`` driven by real headless Chromium.

Each test loads ``tests/fixtures/strategies/price_range_basic.html`` (or
inline HTML built with the same ``[data-bdd=...]`` attributes that the
selector registry expects) and exercises the real strategy against the
real DOM. Click side-effects are observed by reading the
``data-click-count`` attribute the fixture's click recorder maintains.
"""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page

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


# --- fixture-backed tests (use price_range_basic.html) -------------------


async def test_picks_cheapest_within_band(chromium_context, fixture_url):
    """[dom.price-range-happy]: cheapest surviving row in [100, 300] is clicked once."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("price_range_basic"))

    strategy = PriceRangeStrategy(min_price=100.0, max_price=300.0)
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.price is not None
    assert 100.0 <= chosen.price <= 300.0
    # Cheapest in [100, 300] is the $100 row at idx=1.
    assert chosen.price == 100.0
    assert await _click_count(page, 1) == 1
    # No other row received a click.
    for other in (0, 2, 3, 4):
        assert await _click_count(page, other) == 0


async def test_returns_none_when_no_row_in_band(chromium_context, fixture_url):
    """[dom.price-range-reject]: empty band over the fixture rows yields None."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("price_range_basic"))

    # Band [500, 1000] – the fixture's highest priced row is $400.
    strategy = PriceRangeStrategy(min_price=500.0, max_price=1000.0)
    assert await strategy.pick(page) is None

    for idx in range(5):
        assert await _click_count(page, idx) == 0


# --- inline-rendered targeted behavior tests ------------------------------


async def test_band_inclusive_at_min(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A $30.00"},
            {"text": "Section 200 Row C $50.00"},
            {"text": "Section 300 Row B $100.00"},
        ],
    )
    # min=50 should let the $50 row through (inclusive) and pick it
    # because it's cheaper than the $100 row.
    strategy = PriceRangeStrategy(min_price=50.0, max_price=200.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 50.0
    assert await _click_count(page, 1) == 1


async def test_band_inclusive_at_max(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A $200.00"},
            {"text": "Section 200 Row C $300.00"},
        ],
    )
    # max=200 must include the $200 row (inclusive). Cheapest surviving
    # is the $200 row.
    strategy = PriceRangeStrategy(min_price=0.0, max_price=200.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 200.0
    assert await _click_count(page, 0) == 1
    assert await _click_count(page, 1) == 0


async def test_max_only_acts_like_cheapest_under_cap(chromium_context):
    """When ``min_price`` is None, only ``max_price`` constrains the set."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 1 $80.00"},
            {"text": "Section 2 $120.00"},
            {"text": "Section 3 $400.00"},
        ],
    )
    strategy = PriceRangeStrategy(min_price=None, max_price=150.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 80.0
    assert await _click_count(page, 0) == 1


async def test_returns_none_on_empty_list(chromium_context):
    page = await chromium_context.new_page()
    await page.set_content(
        '<!doctype html><html><body><main role="main">'
        '<ul data-bdd="quick-picks-list"></ul>'
        "</main></body></html>"
    )
    strategy = PriceRangeStrategy(min_price=0.0, max_price=1000.0)
    assert await strategy.pick(page) is None
