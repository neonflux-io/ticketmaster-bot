"""Tests for :class:`ScreenshotOnFailureHook`.

Covers the contract in [io.screenshot-on-failure-writes]: when a real
Playwright run hits ``on_failure``, the hook must drop a ``screenshot.png``,
``dom.html`` and ``console.log`` under
``<base_dir>/run-<UTC>-<account>/`` with non-zero file sizes.

Every test in this module drives real headless Chromium via the
``chromium_context`` fixture from ``tests/conftest.py``. No mocks, no fakes -
the hook is invoked exactly as the production lifecycle dispatcher would
invoke it.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import BrowserContext

from src.hooks.base import HookRegistry
from src.hooks.screenshot_on_failure import ScreenshotOnFailureHook
from src.orchestrator.lifecycle import LifecycleDispatcher

_RUN_DIR_RE = re.compile(r"^run-\d{8}T\d{6}Z-[A-Za-z0-9_.-]+$")


# ----- filesystem helpers (sync; called via asyncio.to_thread from tests) -----


def _match_run_dirs(base: Path, suffix: str) -> list[Path]:
    """Return run-<UTC>-<account>/ directories under ``base`` matching ``suffix``."""
    return [
        p
        for p in base.iterdir()
        if p.is_dir() and _RUN_DIR_RE.match(p.name) and p.name.endswith(suffix)
    ]


def _artifact_info(screenshot: Path, dom: Path, console_log: Path) -> dict[str, object]:
    """Read artifact metadata + bodies in a single thread hop."""
    return {
        "screenshot_exists": screenshot.is_file(),
        "dom_exists": dom.is_file(),
        "console_exists": console_log.is_file(),
        "screenshot_size": screenshot.stat().st_size if screenshot.is_file() else 0,
        "dom_size": dom.stat().st_size if dom.is_file() else 0,
        "console_size": console_log.stat().st_size if console_log.is_file() else 0,
        "screenshot_head": screenshot.read_bytes()[:8] if screenshot.is_file() else b"",
        "dom_text": dom.read_text(encoding="utf-8") if dom.is_file() else "",
        "console_text": console_log.read_text(encoding="utf-8") if console_log.is_file() else "",
    }


def _file_size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _dir_listing(path: Path) -> list[Path]:
    return list(path.iterdir())


async def _trigger_console_messages(page) -> None:  # noqa: ANN001 - playwright Page
    """Emit a few real ``console.log`` calls so the listener has something to record."""
    await page.evaluate(
        "() => { "
        "console.log('hello-from-failure-scenario'); "
        "console.warn('warned-from-failure-scenario'); "
        "}"
    )
    # Console events are delivered asynchronously over CDP; yield the loop
    # so the listener registered by the hook has a chance to run before we
    # fire ``on_failure``.
    await asyncio.sleep(0.1)


async def test_screenshot_on_failure_writes_all_three_files(
    chromium_context: BrowserContext, tmp_path: Path
) -> None:
    """The hook drops screenshot.png, dom.html and console.log on failure."""
    page = await chromium_context.new_page()
    await page.set_content(
        "<html><body><h1 data-bdd='evt-title'>Forced Failure Fixture</h1>"
        "<p id='body'>placeholder body content</p></body></html>"
    )

    hook = ScreenshotOnFailureHook(base_dir=tmp_path)
    reg = HookRegistry()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    # Setup: hand the hook the live BrowserContext + Page so it can attach
    # its console listener exactly as it would in production.
    await dispatcher.fire(
        "on_run_start",
        None,
        context=chromium_context,
        page=page,
        account="alice",
    )
    await _trigger_console_messages(page)

    # Force the failure event.
    await dispatcher.fire(
        "on_failure",
        None,
        error=RuntimeError("intentional failure for screenshot dump"),
        page=page,
        account="alice",
    )

    matched_dirs = await asyncio.to_thread(_match_run_dirs, tmp_path, "-alice")
    assert len(matched_dirs) == 1, (
        f"expected exactly one run-<UTC>-alice/ directory, found {matched_dirs!r}"
    )
    run_dir = matched_dirs[0]

    screenshot = run_dir / "screenshot.png"
    dom = run_dir / "dom.html"
    console_log = run_dir / "console.log"
    info = await asyncio.to_thread(_artifact_info, screenshot, dom, console_log)
    assert info["screenshot_exists"], f"missing screenshot at {screenshot}"
    assert info["dom_exists"], f"missing dom dump at {dom}"
    assert info["console_exists"], f"missing console log at {console_log}"
    assert info["screenshot_size"] > 0
    assert info["dom_size"] > 0
    assert info["console_size"] > 0

    # screenshot.png is a real PNG: starts with the 8-byte PNG signature.
    assert info["screenshot_head"] == b"\x89PNG\r\n\x1a\n"
    # dom.html captures the live document.
    assert "Forced Failure Fixture" in info["dom_text"]
    # console.log includes both messages emitted while the listener was live.
    assert "hello-from-failure-scenario" in info["console_text"]
    assert "warned-from-failure-scenario" in info["console_text"]


async def test_screenshot_on_failure_run_dir_name_matches_utc_account_format(
    chromium_context: BrowserContext, tmp_path: Path
) -> None:
    """Run directory name embeds a UTC timestamp and the account name."""
    fixed_now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=timezone.utc)
    hook = ScreenshotOnFailureHook(base_dir=tmp_path, clock=lambda: fixed_now)
    reg = HookRegistry()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    page = await chromium_context.new_page()
    await page.set_content("<html><body>x</body></html>")

    await dispatcher.fire(
        "on_run_start",
        None,
        context=chromium_context,
        page=page,
        account="bob",
    )
    await dispatcher.fire(
        "on_failure",
        None,
        error=RuntimeError("boom"),
        page=page,
        account="bob",
    )

    run_dir = tmp_path / "run-20260519T123456Z-bob"
    sizes = await asyncio.to_thread(
        lambda: {
            "dir": run_dir.is_dir(),
            "shot": _file_size(run_dir / "screenshot.png"),
            "dom": _file_size(run_dir / "dom.html"),
            "console": _file_size(run_dir / "console.log"),
        }
    )
    assert sizes["dir"], f"expected directory {run_dir} to be created"
    assert sizes["shot"] > 0
    assert sizes["dom"] > 0
    assert sizes["console"] > 0


async def test_screenshot_on_failure_ignores_non_failure_events(
    chromium_context: BrowserContext, tmp_path: Path
) -> None:
    """No artifacts are written for events other than ``on_failure``."""
    hook = ScreenshotOnFailureHook(base_dir=tmp_path)
    reg = HookRegistry()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    page = await chromium_context.new_page()
    await page.set_content("<html><body>noop</body></html>")
    await dispatcher.fire(
        "on_run_start",
        None,
        context=chromium_context,
        page=page,
        account="zoe",
    )
    await dispatcher.fire(
        "after_select",
        None,
        candidate="row-9",
    )
    await dispatcher.fire(
        "on_run_end",
        None,
        success=True,
    )

    listing = await asyncio.to_thread(_dir_listing, tmp_path)
    assert listing == [], f"unexpected artifacts written outside of on_failure: {listing!r}"


async def test_screenshot_on_failure_falls_back_to_runner_account(
    chromium_context: BrowserContext, tmp_path: Path
) -> None:
    """When ``account`` is absent from kwargs, the hook reads it from the runner ctx."""

    class _RunnerWithAccount:
        class _Acc:
            name = "from-ctx"

        account = _Acc()

    hook = ScreenshotOnFailureHook(base_dir=tmp_path)
    reg = HookRegistry()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    page = await chromium_context.new_page()
    await page.set_content("<html><body>fallback</body></html>")

    await dispatcher.fire(
        "on_run_start",
        _RunnerWithAccount(),
        context=chromium_context,
        page=page,
    )
    await dispatcher.fire(
        "on_failure",
        _RunnerWithAccount(),
        error=RuntimeError("boom"),
        page=page,
    )

    matched = await asyncio.to_thread(_match_run_dirs, tmp_path, "-from-ctx")
    assert len(matched) == 1
    size = await asyncio.to_thread(_file_size, matched[0] / "screenshot.png")
    assert size > 0


async def test_screenshot_on_failure_handles_missing_page_without_raising(
    tmp_path: Path,
) -> None:
    """The hook logs and continues when no page is available; never raises."""
    hook = ScreenshotOnFailureHook(base_dir=tmp_path)
    # Direct invocation - no setup events, no kwargs.page.
    await hook.on_event(None, "on_failure", error=RuntimeError("no page here"))
    # No artifacts written, no exception raised.
    listing = await asyncio.to_thread(_dir_listing, tmp_path)
    assert listing == []


async def test_screenshot_on_failure_console_listener_captures_context_level_logs(
    chromium_context: BrowserContext, tmp_path: Path
) -> None:
    """Messages logged from a *second* page in the same context are captured."""
    hook = ScreenshotOnFailureHook(base_dir=tmp_path)
    reg = HookRegistry()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    page = await chromium_context.new_page()
    await page.set_content("<html><body>page-1</body></html>")
    await dispatcher.fire(
        "on_run_start",
        None,
        context=chromium_context,
        page=page,
        account="ctx",
    )

    other = await chromium_context.new_page()
    await other.set_content("<html><body>page-2</body></html>")
    await other.evaluate("() => console.log('from-second-page')")
    await asyncio.sleep(0.1)

    await dispatcher.fire(
        "on_failure",
        None,
        error=RuntimeError("boom"),
        page=page,
        account="ctx",
    )

    run_dirs = await asyncio.to_thread(_match_run_dirs, tmp_path, "-ctx")
    assert len(run_dirs) == 1
    console_text = await asyncio.to_thread(
        (run_dirs[0] / "console.log").read_text, encoding="utf-8"
    )
    assert "from-second-page" in console_text
