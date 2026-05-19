"""Tests for navigator state detection + naive datetime handling.

Drives real headless Chromium against ``tests/fixtures/strategies/*.html``
files for every ``detect_state`` branch. The naive-datetime guard in
``wait_until_on_sale`` is pure-Python and exercised against a freshly-opened
blank page.
"""

from __future__ import annotations

from datetime import datetime

import pytest

# Import via the legacy ``src.bot.navigator`` shim so [refactor.bot-shim]
# stays exercised by the test suite.
from src.bot.navigator import NavigationError, detect_state, wait_until_on_sale


async def test_detect_state_queue_by_url(chromium_context, fixture_url):
    """tests/fixtures/strategies/queue_page.html — URL contains 'queue'."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("queue_page"))
    assert await detect_state(page) == "queue"


async def test_detect_state_sold_out(chromium_context, fixture_url):
    """tests/fixtures/strategies/sold_out.html — page shows the sold-out marker."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("sold_out"))
    assert await detect_state(page) == "sold_out"


async def test_detect_state_not_on_sale(chromium_context, fixture_url):
    """tests/fixtures/strategies/not_on_sale.html — page shows the presale marker."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("not_on_sale"))
    assert await detect_state(page) == "not_on_sale"


async def test_detect_state_tickets(chromium_context, fixture_url):
    """tests/fixtures/strategies/quick_picks_basic.html — quick picks present."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("quick_picks_basic"))
    assert await detect_state(page) == "tickets"


async def test_detect_state_unknown(chromium_context):
    """A page with only an empty ``<main>`` should classify as 'unknown'."""
    page = await chromium_context.new_page()
    await page.set_content(
        '<!doctype html><html><body><main role="main"><h1>Hello</h1></main></body></html>'
    )
    assert await detect_state(page) == "unknown"


async def test_wait_until_on_sale_rejects_naive(chromium_context):
    page = await chromium_context.new_page()
    await page.goto("about:blank")
    naive = datetime(2026, 6, 1, 10, 0, 0)
    with pytest.raises(NavigationError, match="timezone-aware"):
        await wait_until_on_sale(page, naive)
