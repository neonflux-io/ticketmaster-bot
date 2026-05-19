"""Tests for ``ResaleFilterStrategy`` driven by real headless Chromium.

The fixture-backed tests load committed HTML files
(``resale.html`` and ``resale_all_resale.html``) and exercise the
real strategy against the real DOM. Click side-effects are observed by
reading the ``data-click-count`` attribute the fixture's click
recorder maintains.
"""

from __future__ import annotations

import pytest
from playwright.async_api import BrowserContext, Page

from src.strategies.cheapest import CheapestStrategy
from src.strategies.resale_filter import ResaleFilterStrategy


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


def _row_html(idx: int, text: str, *, resale: bool) -> str:
    extra = '<span data-bdd="resale-tag">Resale</span>' if resale else ""
    return (
        f'      <li data-bdd="quick-pick-row" data-idx="{idx}">{text}{extra}</li>'
    )


async def _page_with_rows(
    context: BrowserContext, rows: list[tuple[str, bool]]
) -> Page:
    items = "\n".join(_row_html(i, text, resale=resale) for i, (text, resale) in enumerate(rows))
    html = (
        '<!doctype html><html><body><main role="main">'
        f'<ul data-bdd="quick-picks-list">\n{items}\n</ul>'
        "</main>"
        f"<script>{_click_recorder_script()}</script>"
        "</body></html>"
    )
    page = await context.new_page()
    await page.set_content(html)
    return page


async def _click_count(page: Page, idx: int) -> int:
    return int(
        await page.locator(
            f'[data-bdd="quick-pick-row"][data-idx="{idx}"]'
        ).get_attribute("data-click-count")
        or "0"
    )


# --- fixture-backed assertions --------------------------------------------


async def test_exclude_resale_delegates_to_inner(chromium_context, fixture_url):
    """[dom.resale-filter-delegate]: with ``exclude_resale=True`` against a
    fixture whose cheapest non-resale row is idx=0 ($80), the wrapper
    returns the same candidate ``CheapestStrategy()`` alone would have
    picked from the surviving (non-resale) subset.

    Plain ``CheapestStrategy()`` would have picked the $30 resale row
    (idx=4); the wrapper must hide it.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("resale"))

    strategy = ResaleFilterStrategy(
        inner=CheapestStrategy(),
        exclude_resale=True,
    )
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.price == 80.0
    assert chosen.section == "100"
    assert await _click_count(page, 0) == 1
    for other in (1, 2, 3, 4):
        assert await _click_count(page, other) == 0


async def test_exclude_resale_returns_none_when_all_resale(chromium_context, fixture_url):
    """[dom.resale-filter-reject]: every fixture row carries
    ``[data-bdd='resale-tag']`` so the filter strips them all and
    nothing is clicked.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("resale_all_resale"))

    strategy = ResaleFilterStrategy(
        inner=CheapestStrategy(),
        exclude_resale=True,
    )
    chosen = await strategy.pick(page)

    assert chosen is None
    for idx in range(3):
        assert await _click_count(page, idx) == 0


async def test_include_resale_keeps_only_resale_rows(chromium_context, fixture_url):
    """With ``include_resale=True`` the wrapper keeps only resale rows
    and delegates the cheapest pick to the inner strategy.

    In ``resale.html`` the cheapest resale row is idx=4 ($30).
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("resale"))

    strategy = ResaleFilterStrategy(
        inner=CheapestStrategy(),
        include_resale=True,
    )
    chosen = await strategy.pick(page)

    assert chosen is not None
    assert chosen.price == 30.0
    assert await _click_count(page, 4) == 1
    for other in (0, 1, 2, 3):
        assert await _click_count(page, other) == 0


# --- inline-rendered behavior tests ---------------------------------------


async def test_exclude_resale_passthrough_when_no_resale_present(chromium_context):
    """When no row carries a resale tag, ``exclude_resale=True`` is a
    pure passthrough and the inner strategy's pick is returned.
    """
    page = await _page_with_rows(
        chromium_context,
        [
            ("Section 100 Row A — $200.00", False),
            ("Section 200 Row B — $50.00", False),
            ("Section 300 Row C — $400.00", False),
        ],
    )
    strategy = ResaleFilterStrategy(
        inner=CheapestStrategy(),
        exclude_resale=True,
    )
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 50.0
    assert await _click_count(page, 1) == 1


async def test_include_resale_skips_non_resale_rows(chromium_context):
    """``include_resale=True`` drops every row that lacks a resale tag.

    With one resale row and one non-resale row, only the resale row
    survives the filter and gets clicked.
    """
    page = await _page_with_rows(
        chromium_context,
        [
            ("Section 100 Row A — $200.00", False),
            ("Section 200 Row B — $50.00", True),
        ],
    )
    strategy = ResaleFilterStrategy(
        inner=CheapestStrategy(),
        include_resale=True,
    )
    chosen = await strategy.pick(page)
    assert chosen is not None
    assert chosen.price == 50.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0


async def test_include_resale_returns_none_when_no_resale_rows(chromium_context):
    page = await _page_with_rows(
        chromium_context,
        [
            ("Section 100 Row A — $200.00", False),
            ("Section 200 Row B — $50.00", False),
        ],
    )
    strategy = ResaleFilterStrategy(
        inner=CheapestStrategy(),
        include_resale=True,
    )
    assert await strategy.pick(page) is None
    for idx in (0, 1):
        assert await _click_count(page, idx) == 0


def test_resale_filter_rejects_both_flags_set() -> None:
    """Setting both ``include_resale`` and ``exclude_resale`` is a
    config mistake — surface it loudly."""
    with pytest.raises(ValueError):
        ResaleFilterStrategy(
            inner=CheapestStrategy(),
            include_resale=True,
            exclude_resale=True,
        )


def test_resale_filter_rejects_neither_flag_set() -> None:
    """Setting neither flag means the wrapper has no work to do —
    that's almost certainly a misconfiguration."""
    with pytest.raises(ValueError):
        ResaleFilterStrategy(inner=CheapestStrategy())


def test_resale_filter_registered_in_strategy_registry() -> None:
    """The strategy must be discoverable by name via the singleton
    ``StrategyRegistry`` so external code can build it generically."""
    from src.registry import strategies as strategy_registry
    from src.strategies import factory  # noqa: F401  (import for side effects)

    assert "resale_filter" in strategy_registry.registry
    assert strategy_registry.registry.get("resale_filter") is ResaleFilterStrategy
