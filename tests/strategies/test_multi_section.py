"""Tests for ``MultiSectionStrategy`` driven by real headless Chromium."""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page

from src.strategies.multi_section import MultiSectionStrategy


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


async def test_first_preference_wins_over_later_preferences(chromium_context, fixture_url):
    """[dom.multi-section-happy]: section 100 wins over 200 even though
    section 200 contains a cheaper $60 row in the fixture.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("multi_section_basic"))

    strategy = MultiSectionStrategy(sections=["100", "200"])
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.section == "100"
    # Cheapest section-100 row is $150 at idx=2.
    assert chosen.price == 150.0
    assert await _click_count(page, 2) == 1
    for other in (0, 1, 3, 4):
        assert await _click_count(page, other) == 0


async def test_returns_none_when_no_preference_matches(chromium_context, fixture_url):
    """[dom.multi-section-reject]: section list missing every row's section."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("multi_section_basic"))

    strategy = MultiSectionStrategy(sections=["999"])
    assert await strategy.pick(page) is None

    for idx in range(5):
        assert await _click_count(page, idx) == 0


# --- inline-rendered behavior tests ---------------------------------------


async def test_falls_through_to_next_preference_when_first_missing(chromium_context):
    """When the first-preference section is absent, the next one is tried."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 200 Row A $80.00"},
            {"text": "Section 200 Row C $100.00"},
            {"text": "Section 300 Row B $60.00"},
        ],
    )
    strategy = MultiSectionStrategy(sections=["100", "200"])
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "200"
    # Cheapest section-200 row is $80 at idx=0.
    assert chosen.price == 80.0
    assert await _click_count(page, 0) == 1
    assert await _click_count(page, 1) == 0
    assert await _click_count(page, 2) == 0


async def test_cheapest_within_preferred_section_when_multiple_match(chromium_context):
    """If several rows share the preferred section, pick the cheapest."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A $200.00"},
            {"text": "Section 100 Row B $90.00"},
            {"text": "Section 100 Row C $150.00"},
            {"text": "Section 200 Row D $50.00"},
        ],
    )
    strategy = MultiSectionStrategy(sections=["100", "200"])
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "100"
    assert chosen.price == 90.0
    assert await _click_count(page, 1) == 1


async def test_max_price_applied_per_preference(chromium_context):
    """When ``max_price`` is set, sections whose rows all exceed it are
    skipped and the next preference is tried."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A $500.00"},
            {"text": "Section 100 Row B $600.00"},
            {"text": "Section 200 Row C $80.00"},
            {"text": "Section 200 Row D $90.00"},
        ],
    )
    strategy = MultiSectionStrategy(sections=["100", "200"], max_price=200.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.section == "200"
    assert chosen.price == 80.0
    assert await _click_count(page, 2) == 1
    assert await _click_count(page, 0) == 0
    assert await _click_count(page, 1) == 0


async def test_returns_none_when_empty_candidate_list(chromium_context):
    page = await chromium_context.new_page()
    await page.set_content(
        '<!doctype html><html><body><main role="main">'
        '<ul data-bdd="quick-picks-list"></ul>'
        "</main></body></html>"
    )
    strategy = MultiSectionStrategy(sections=["100"])
    assert await strategy.pick(page) is None
