"""Tests for ``SeatQualityStrategy`` driven by real headless Chromium."""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page

from src.strategies.seat_quality import SeatQualityStrategy


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


def _row_html(idx: int, text: str, *, score: str | None = None) -> str:
    score_attr = f' data-quality-score="{score}"' if score is not None else ""
    return f'      <li data-bdd="quick-pick-row" data-idx="{idx}"{score_attr}>{text}</li>'


async def _page_with_html(context: BrowserContext, rows_html: str) -> Page:
    html = (
        '<!doctype html><html><body><main role="main">'
        '<ul data-bdd="quick-picks-list">'
        f"{rows_html}"
        "</ul>"
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


async def test_seat_quality_picks_highest_scored_row(chromium_context, fixture_url):
    """[dom.seat-quality-happy]: pick the highest-scored row.

    In the fixture, idx=3 has ``data-quality-score="9"`` plus tokens
    floor+center+front+aisle in its text — it must win.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("seat_quality"))

    strategy = SeatQualityStrategy()
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.price == 400.0
    assert await _click_count(page, 3) == 1
    for other in (0, 1, 2, 4):
        assert await _click_count(page, other) == 0


# --- inline-rendered behavior tests ---------------------------------------


async def test_seat_quality_token_scoring_floor_center(chromium_context):
    """Rows with ``floor`` + ``center`` tokens outscore rows without."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 500 Row Z — Upper deck — $40.00")
        + _row_html(1, "Section 100 Row A — Floor Center — $250.00"),
    )
    strategy = SeatQualityStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 250.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0


async def test_seat_quality_data_attribute_dominates(chromium_context):
    """A high ``data-quality-score`` beats text tokens alone."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 Row A — Floor Center Aisle Front — $300.00")
        + _row_html(1, "Section 200 Row C — Upper deck — $200.00", score="99"),
    )
    strategy = SeatQualityStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    # idx=1 should win because score 99 outweighs all four tokens.
    assert chosen.price == 200.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0


async def test_seat_quality_token_case_insensitive(chromium_context):
    """Tokens are matched case-insensitively (``AISLE``, ``Aisle``, ``aisle``)."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 Row A — $100.00")
        + _row_html(1, "Section 200 Row C — AISLE seat — $150.00"),
    )
    strategy = SeatQualityStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 150.0
    assert await _click_count(page, 1) == 1


async def test_seat_quality_tie_broken_by_cheaper_price(chromium_context):
    """When two rows tie on score, the cheaper one wins (tie-breaker)."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 Row A — Floor Center — $500.00")
        + _row_html(1, "Section 200 Row C — Floor Center — $200.00"),
    )
    strategy = SeatQualityStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 200.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0


async def test_seat_quality_falls_back_when_no_signals(chromium_context):
    """With zero tokens and no quality-score on every row, the strategy
    still must pick *something* — fall back to the first row so the bot
    isn't stuck."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 Row A — $80.00") + _row_html(1, "Section 200 Row C — $90.00"),
    )
    strategy = SeatQualityStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    # Both have score 0; the cheaper one wins via the tie-breaker.
    assert chosen.price == 80.0
    assert await _click_count(page, 0) == 1


async def test_seat_quality_max_price_excludes_pricey(chromium_context):
    """``max_price`` is honored: a high-quality but over-budget row is skipped."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 500 Row Z — Upper deck — $40.00")
        + _row_html(1, "Section 100 Row A — Floor Center — $400.00", score="9"),
    )
    strategy = SeatQualityStrategy(max_price=200.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    # idx=1 over budget; idx=0 wins by default.
    assert chosen.price == 40.0
    assert await _click_count(page, 0) == 1
    assert await _click_count(page, 1) == 0


async def test_seat_quality_max_price_rejects_all_returns_none(chromium_context):
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 — Floor Center — $500.00"),
    )
    strategy = SeatQualityStrategy(max_price=100.0)
    assert await strategy.pick(page) is None
    assert await _click_count(page, 0) == 0


async def test_seat_quality_empty_quick_picks_returns_none(chromium_context, fixture_url):
    """[dom.empty-candidates-none]: empty fixture → None, no clicks."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("empty_quick_picks"))

    strategy = SeatQualityStrategy()
    assert await strategy.pick(page) is None
