"""Tests for ``AccessibleStrategy`` driven by real headless Chromium.

Each test loads ``tests/fixtures/strategies/accessible.html`` (or inline
HTML built with the same selector attributes) and exercises the real
strategy against the real DOM. Click side-effects are observed by
reading the ``data-click-count`` attribute the fixture's click recorder
maintains.
"""

from __future__ import annotations

from playwright.async_api import BrowserContext, Page

from src.strategies.accessible import AccessibleStrategy


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


def _row_html(idx: int, text: str, *, marker: str = "", extra_attrs: str = "") -> str:
    return f'      <li data-bdd="quick-pick-row" data-idx="{idx}"{extra_attrs}>{text}{marker}</li>'


async def _page_with_html(context: BrowserContext, body_inner: str) -> Page:
    html = (
        '<!doctype html><html><body><main role="main">'
        '<ul data-bdd="quick-picks-list">'
        f"{body_inner}"
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


async def test_accessible_picks_accessible_row(chromium_context, fixture_url):
    """[dom.accessible-happy]: the strategy must return a row carrying an
    accessible marker (``[data-bdd='accessible-seat']`` or
    ``aria-label*='accessible' i``) and click it exactly once.

    The fixture's two accessible rows are idx=2 ($250 via a child marker)
    and idx=3 ($300 via aria-label on the row itself). The cheaper one
    (idx=2) wins.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("accessible"))

    strategy = AccessibleStrategy()
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.price == 250.0
    assert await _click_count(page, 2) == 1
    for other in (0, 1, 3, 4):
        assert await _click_count(page, other) == 0


async def test_accessible_returns_none_when_no_accessible_rows(chromium_context, fixture_url):
    """[dom.accessible-reject]: zero accessible rows ⇒ pick() is None."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("quick_picks_basic"))

    strategy = AccessibleStrategy()
    assert await strategy.pick(page) is None

    for idx in range(5):
        assert await _click_count(page, idx) == 0


# --- inline-rendered behavior tests ---------------------------------------


async def test_accessible_via_data_bdd_marker_on_row_itself(chromium_context):
    """The row element itself carries ``data-bdd='accessible-seat'``.

    Falls back to the ``.quick-picks li`` selector for candidate listing
    so the row carries the accessible-seat marker as its primary
    ``data-bdd`` value (which is how Ticketmaster's DOM marks
    wheelchair-only rows when they aren't in the standard
    quick-pick-row pool).
    """
    page = await chromium_context.new_page()
    html = (
        '<!doctype html><html><body><main role="main">'
        '<ul class="quick-picks">'
        '<li data-bdd="accessible-seat" data-idx="0">Section 200 Row C — $200.00</li>'
        '<li data-bdd="quick-pick-row" data-idx="1">Section 100 Row A — $150.00</li>'
        "</ul></main>"
        "<script>"
        "window.__clicks = [];"
        "document.addEventListener('click', function (event) {"
        "  var row = event.target.closest('[data-idx]');"
        "  if (!row) return;"
        "  window.__clicks.push(row.getAttribute('data-idx'));"
        "  var prev = parseInt(row.getAttribute('data-click-count') || '0', 10);"
        "  row.setAttribute('data-click-count', String(prev + 1));"
        "}, true);"
        "</script></body></html>"
    )
    await page.set_content(html)
    strategy = AccessibleStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 200.0
    # The accessible row (idx=0) was clicked exactly once.
    idx0_count = int(await page.locator('[data-idx="0"]').get_attribute("data-click-count") or "0")
    idx1_count = int(await page.locator('[data-idx="1"]').get_attribute("data-click-count") or "0")
    assert idx0_count == 1
    assert idx1_count == 0


async def test_accessible_via_aria_label_on_row(chromium_context):
    """``aria-label*='accessible' i`` on the row matches case-insensitively."""
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 Row A — $80.00")
        + _row_html(
            1,
            "Section 200 Row C — $300.00",
            extra_attrs=' aria-label="ACCESSIBLE — wheelchair section"',
        ),
    )
    strategy = AccessibleStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 300.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0


async def test_accessible_picks_cheapest_among_accessible(chromium_context):
    """When multiple rows qualify, the cheapest one wins."""
    page = await _page_with_html(
        chromium_context,
        _row_html(
            0,
            "Section 100 Row A — $400.00",
            marker='<span data-bdd="accessible-seat"> ♿</span>',
        )
        + _row_html(
            1,
            "Section 200 Row C — $150.00",
            extra_attrs=' aria-label="Accessible aisle seat"',
        )
        + _row_html(2, "Section 300 Row D — $50.00"),
    )
    strategy = AccessibleStrategy()
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 150.0
    assert await _click_count(page, 1) == 1
    for other in (0, 2):
        assert await _click_count(page, other) == 0


async def test_accessible_flag_false_disables_filter(chromium_context):
    """When ``accessible_seats=False`` (the legacy default), the strategy
    behaves like a cheapest-pick — accessibility is no longer required.

    This is the contract behind "honors the existing
    ``tickets.accessible_seats`` flag" — if the flag is off, the user
    didn't ask for accessible seats and the strategy must not reject
    inaccessible rows.
    """
    page = await _page_with_html(
        chromium_context,
        _row_html(0, "Section 100 Row A — $80.00") + _row_html(1, "Section 200 Row C — $120.00"),
    )
    strategy = AccessibleStrategy(accessible_seats=False)
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 80.0
    assert await _click_count(page, 0) == 1


async def test_accessible_max_price_excludes_pricey_rows(chromium_context):
    """``max_price`` is honored when set, even after the accessible filter."""
    page = await _page_with_html(
        chromium_context,
        _row_html(
            0,
            "Section 100 Row A — $400.00",
            extra_attrs=' aria-label="Accessible aisle seat"',
        )
        + _row_html(
            1,
            "Section 200 Row C — $600.00",
            marker='<span data-bdd="accessible-seat"> ♿</span>',
        ),
    )
    strategy = AccessibleStrategy(max_price=300.0)
    assert await strategy.pick(page) is None
    for idx in (0, 1):
        assert await _click_count(page, idx) == 0


async def test_accessible_empty_quick_picks_returns_none(chromium_context, fixture_url):
    """[dom.empty-candidates-none]: with zero rows in the fixture, return None."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("empty_quick_picks"))

    strategy = AccessibleStrategy()
    assert await strategy.pick(page) is None
