"""Tests for ``RandomPickStrategy`` driven by real headless Chromium.

The fixture-backed tests load committed HTML files (``quick_picks_basic``
and ``empty_quick_picks``) and exercise the real strategy against the
real DOM. Click side-effects are observed by reading the
``data-click-count`` attribute the fixture's click recorder maintains.
"""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page

from src.strategies.random_pick import RandomPickStrategy


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


# --- fixture-backed deterministic-seed test --------------------------------


async def test_random_pick_is_deterministic_with_same_seed(chromium_context, fixture_url):
    """[dom.random-pick-deterministic]: same seed across two runs picks
    the same row (by description) and clicks it exactly once each run.

    ``quick_picks_basic.html`` has 5 quick-pick rows, satisfying the
    assertion's "≥5 rows" requirement.
    """
    # Run 1.
    page_a = await chromium_context.new_page()
    await page_a.goto(fixture_url("quick_picks_basic"))
    strategy_a = RandomPickStrategy(seed=42)
    chosen_a = await strategy_a.pick(page_a)
    assert chosen_a is not None
    # Exactly one row clicked, somewhere on the page.
    clicks_a = await page_a.evaluate("() => window.__clicks.length")
    assert clicks_a == 1

    # Run 2: fresh page, fresh strategy, same seed.
    page_b = await chromium_context.new_page()
    await page_b.goto(fixture_url("quick_picks_basic"))
    strategy_b = RandomPickStrategy(seed=42)
    chosen_b = await strategy_b.pick(page_b)
    assert chosen_b is not None
    clicks_b = await page_b.evaluate("() => window.__clicks.length")
    assert clicks_b == 1

    assert chosen_a.description == chosen_b.description


async def test_random_pick_empty_quick_picks_returns_none(chromium_context, fixture_url):
    """[dom.empty-candidates-none]: empty fixture ⇒ None, no clicks."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("empty_quick_picks"))

    strategy = RandomPickStrategy(seed=7)
    assert await strategy.pick(page) is None


# --- inline-rendered behavior tests ---------------------------------------


async def test_random_pick_skips_rows_over_max_price(chromium_context):
    """``max_price`` excludes pricey rows; the first survivor under the
    cap (in shuffled order) is the one that gets clicked."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — $500.00"},
            {"text": "Section 200 Row C — $800.00"},
            {"text": "Section 300 Row D — $1,000.00"},
            {"text": "Section 400 Row B — $80.00"},
        ],
    )
    strategy = RandomPickStrategy(seed=1, max_price=200.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    # Only row idx=3 is under $200.
    assert chosen.price == 80.0
    assert await _click_count(page, 3) == 1
    for other in (0, 1, 2):
        assert await _click_count(page, other) == 0


async def test_random_pick_returns_none_when_all_over_max_price(chromium_context):
    """When every row exceeds ``max_price``, pick() returns None."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — $500.00"},
            {"text": "Section 200 Row C — $800.00"},
        ],
    )
    strategy = RandomPickStrategy(seed=1, max_price=100.0)
    assert await strategy.pick(page) is None
    for idx in (0, 1):
        assert await _click_count(page, idx) == 0


async def test_random_pick_different_seeds_can_pick_different_rows(chromium_context):
    """With a non-trivial candidate set, two different seeds must be able
    to produce two different picks — proves the shuffle isn't a no-op.

    We try multiple seeds; the moment we see two distinct picks, the
    invariant holds.
    """
    rows = [
        {"text": "Section 100 Row A — $10.00"},
        {"text": "Section 200 Row B — $20.00"},
        {"text": "Section 300 Row C — $30.00"},
        {"text": "Section 400 Row D — $40.00"},
        {"text": "Section 500 Row E — $50.00"},
    ]
    picks: set[str] = set()
    for seed in range(20):
        page = await _page_with_rows(chromium_context, rows)
        chosen = await RandomPickStrategy(seed=seed).pick(page)
        assert chosen is not None
        picks.add(chosen.description)
        await page.close()
        if len(picks) >= 2:
            break
    assert len(picks) >= 2


async def test_random_pick_includes_rows_with_unknown_price(chromium_context):
    """Rows whose price could not be parsed remain in the candidate pool
    when no ``max_price`` is supplied."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — TBA"},
            {"text": "Section 200 Row B — Free entry"},
        ],
    )
    strategy = RandomPickStrategy(seed=2)
    chosen = await strategy.pick(page)
    assert chosen is not None
    # Exactly one row clicked, somewhere on the page.
    total_clicks = await page.evaluate("() => window.__clicks.length")
    assert total_clicks == 1


async def test_random_pick_excludes_unknown_price_rows_when_max_set(chromium_context):
    """With ``max_price`` set, rows without a parsed price are skipped."""
    page = await _page_with_rows(
        chromium_context,
        [
            {"text": "Section 100 Row A — TBA"},
            {"text": "Section 200 Row B — Free entry"},
            {"text": "Section 300 Row C — $80.00"},
        ],
    )
    strategy = RandomPickStrategy(seed=3, max_price=200.0)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 80.0
    assert await _click_count(page, 2) == 1
