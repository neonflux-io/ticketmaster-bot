"""Shared pytest fixtures for real-Chromium tests.

Provides a session-scoped headless Chromium browser, a function-scoped
:class:`BrowserContext`, and a ``fixture_url`` helper that returns a
``file://`` URL for any HTML asset under ``tests/fixtures/``. No test doubles
are exposed — every DOM test runs against real Playwright.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext

# Make ``src`` importable when running pytest from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_ticketmaster_bot_logger_propagation() -> Iterator[None]:
    """Restore caplog visibility on the ``ticketmaster-bot`` logger.

    ``src.utils.logger.setup_logger`` (exercised by ``tests/test_logger.py``
    and ``tests/utils/test_logger_json.py``) attaches handlers to the
    ``ticketmaster-bot`` logger and sets ``propagate=False``. Because the
    logger is a module-level singleton, that mutation leaks into every
    subsequent test in the same pytest process. pytest's ``caplog`` fixture
    installs its handler on the *root* logger, so once propagation is
    disabled and stale file/console handlers remain attached, log records
    emitted by production code never reach ``caplog`` — making any test
    that asserts on caplog content (e.g. the purchase-guard integration
    tests) fail intermittently depending on collection order.

    Resetting both knobs at function setup guarantees caplog can see every
    record the ``ticketmaster-bot`` logger emits, regardless of which test
    ran before it.
    """
    logger = logging.getLogger("ticketmaster-bot")
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)
    original_level = logger.level
    # Detach any handlers left behind by previous tests (e.g. RichHandler /
    # FileHandler from setup_logger) and re-enable propagation so caplog's
    # root-level handler receives our records.
    logger.handlers.clear()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    try:
        yield
    finally:
        logger.handlers.clear()
        for handler in original_handlers:
            logger.addHandler(handler)
        logger.propagate = original_propagate
        logger.setLevel(original_level)


@pytest_asyncio.fixture(scope="session")
async def chromium() -> AsyncIterator[Browser]:
    """Session-scoped real headless Chromium browser.

    Launched once per pytest session and torn down at the end so the per-test
    ``chromium_context`` fixture only pays the cost of opening a fresh
    browser context (cheap) instead of launching a whole browser.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


@pytest_asyncio.fixture
async def chromium_context(chromium: Browser) -> AsyncIterator[BrowserContext]:
    """Function-scoped fresh BrowserContext.

    Each test gets a brand-new context (which means a fresh cookie jar,
    storage, etc.) that is closed in teardown so nothing leaks between tests.
    """
    context = await chromium.new_context()
    try:
        yield context
    finally:
        await context.close()


@pytest_asyncio.fixture
def fixture_url() -> Callable[[str], str]:
    """Return a helper that builds a ``file://`` URL for an HTML fixture.

    Accepted inputs:

    * Bare name (``"quick_picks_basic"``) — resolved under
      ``tests/fixtures/strategies/<name>.html``.
    * Repo-relative path (``"tests/fixtures/strategies/sold_out.html"``).
    * Fixtures-relative path (``"strategies/event_page.html"``).
    """

    def _url(name: str) -> str:
        candidate = Path(name)
        if candidate.is_absolute():
            full = candidate.resolve()
        elif name.startswith("tests/fixtures/"):
            full = (_REPO_ROOT / candidate).resolve()
        elif candidate.suffix == "":
            full = (_FIXTURES_DIR / "strategies" / f"{name}.html").resolve()
        else:
            full = (_FIXTURES_DIR / candidate).resolve()
        if not full.is_file():
            raise FileNotFoundError(f"Fixture HTML not found: {full}")
        return full.as_uri()

    return _url
