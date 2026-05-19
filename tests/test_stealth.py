"""Real-Chromium tests for :mod:`src.utils.stealth`.

These exercises confirm the stealth init script attaches to a real
``launch_persistent_context`` browser context and successfully patches
the two most obvious automation tells:

* ``navigator.webdriver`` reads as JS ``undefined`` (Python ``None``).
* ``navigator.plugins`` exposes a non-empty plugin array.

The feature contract for this surface lives at
``dom.stealth-webdriver-undef`` in
``mission/validation-contract.md`` — every assertion below maps directly
back to that contract item.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest_asyncio
from playwright.async_api import async_playwright

from src.utils.config_loader import StealthConfig
from src.utils.stealth import apply_stealth

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext


@pytest_asyncio.fixture
async def stealth_context(tmp_path: Path) -> AsyncIterator[BrowserContext]:
    """Real persistent Chromium context with stealth enabled.

    Uses ``launch_persistent_context`` (the same surface
    ``src.vendors.ticketmaster.core`` drives in production) so the init
    script attaches to the same flavour of context that ships at
    runtime. ``tmp_path`` keeps each test isolated and self-cleaning.
    """
    user_data_dir = tmp_path / "stealth-profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--disable-features=AutomationControlled",
            ],
        )
        await apply_stealth(context, StealthConfig(enabled=True))
        try:
            yield context
        finally:
            await context.close()


async def test_stealth_webdriver_is_undefined(
    stealth_context: BrowserContext,
) -> None:
    """[dom.stealth-webdriver-undef] navigator.webdriver -> JS undefined."""
    page = await stealth_context.new_page()
    await page.goto("about:blank")

    webdriver = await page.evaluate("() => navigator.webdriver")
    assert webdriver is None, f"expected undefined navigator.webdriver, got {webdriver!r}"


async def test_stealth_plugins_array_non_empty(
    stealth_context: BrowserContext,
) -> None:
    """[dom.stealth-webdriver-undef] navigator.plugins.length >= 1."""
    page = await stealth_context.new_page()
    await page.goto("about:blank")

    plugin_count = await page.evaluate("() => navigator.plugins.length")
    assert isinstance(plugin_count, int)
    assert plugin_count >= 1, f"expected stealth to expose >=1 fake plugin, got {plugin_count}"


async def test_stealth_disabled_is_noop(tmp_path: Path) -> None:
    """``apply_stealth`` with ``enabled=False`` must not install the script.

    Confirms the early-return path: without the init script,
    ``navigator.plugins`` is whatever Chromium reports natively (an empty
    array in headless mode), proving our shim only runs when opted in.
    """
    user_data_dir = tmp_path / "no-stealth-profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
        )
        try:
            await apply_stealth(context, StealthConfig(enabled=False))
            page = await context.new_page()
            await page.goto("about:blank")
            plugin_count = await page.evaluate("() => navigator.plugins.length")
            assert plugin_count == 0, (
                "headless Chromium without the stealth shim should report "
                f"zero plugins; got {plugin_count}"
            )
        finally:
            await context.close()
