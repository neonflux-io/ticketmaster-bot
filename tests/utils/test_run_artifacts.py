"""Tests for :mod:`src.utils.run_artifacts`.

Covers the validation contract assertion ``io.run-artifacts-complete``: on a
failed run, ``logs/run-<UTC>-<account>/`` must exist and contain
``screenshot.png``, ``dom.html``, ``console.log``, and ``network.har``, each
with ``stat().st_size > 0``.

Every test uses real headless Chromium via the ``chromium`` session-scoped
fixture in ``tests/conftest.py`` — no mocks, no fakes. A fresh browser
context (with HAR recording enabled) is created per test so HAR teardown
runs through Playwright's real flush path.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest_asyncio
from playwright.async_api import Browser, BrowserContext, ConsoleMessage

from src.utils.run_artifacts import RunArtifactDir, dump_console, dump_dom, dump_screenshot

_RUN_DIR_RE = re.compile(r"^run-\d{8}T\d{6}Z-[A-Za-z0-9_.-]+$")


@pytest_asyncio.fixture
async def har_context(chromium: Browser, tmp_path: Path) -> AsyncIterator[BrowserContext]:
    """A fresh BrowserContext that records HAR to ``tmp_path/network.har``.

    The HAR file is only fully written on ``context.close()``, so we close
    the context explicitly in teardown rather than relying on the
    session-scoped browser teardown.
    """
    har_file = tmp_path / "_pre_close_har.har"
    context = await chromium.new_context(record_har_path=str(har_file))
    try:
        yield context
    finally:
        # The tests close the context themselves (so HAR flushes deterministically
        # before they assert on it); make teardown idempotent.
        try:
            await context.close()
        except Exception:  # noqa: BLE001
            pass


async def test_run_artifact_dir_creates_directory_on_first_write(tmp_path: Path) -> None:
    """``ensure()`` creates ``logs/run-<UTC>-<account>/`` lazily."""
    fixed_now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=timezone.utc)
    artifact = RunArtifactDir(base=tmp_path, run_id=fixed_now, account_name="alice")

    expected_dir = tmp_path / "run-20260519T123456Z-alice"
    # No directory is created at construction time — only when ensure() runs.
    assert not expected_dir.exists()

    created = await asyncio.to_thread(artifact.ensure)
    assert created == expected_dir
    assert expected_dir.is_dir()
    # ``ensure()`` is idempotent — calling it twice does not raise.
    again = await asyncio.to_thread(artifact.ensure)
    assert again == expected_dir


async def test_run_artifact_dir_sanitises_account_name(tmp_path: Path) -> None:
    """Account names with unsafe path chars are sanitised before mkdir."""
    fixed_now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=timezone.utc)
    artifact = RunArtifactDir(base=tmp_path, run_id=fixed_now, account_name="bob/with spaces")
    run_dir = await asyncio.to_thread(artifact.ensure)
    assert run_dir.name == "run-20260519T123456Z-bob_with_spaces"


async def test_run_artifact_dir_handles_blank_account(tmp_path: Path) -> None:
    """An empty account name falls back to a stable placeholder."""
    fixed_now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=timezone.utc)
    artifact = RunArtifactDir(base=tmp_path, run_id=fixed_now, account_name="")
    run_dir = await asyncio.to_thread(artifact.ensure)
    assert run_dir.name == "run-20260519T123456Z-unknown"


async def test_run_artifact_path_returns_child_under_run_dir(tmp_path: Path) -> None:
    """``path()`` joins under the resolved run directory."""
    fixed_now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=timezone.utc)
    artifact = RunArtifactDir(base=tmp_path, run_id=fixed_now, account_name="zoe")
    screenshot = artifact.path("screenshot.png")
    har = artifact.path("network.har")
    assert screenshot == tmp_path / "run-20260519T123456Z-zoe" / "screenshot.png"
    assert har == tmp_path / "run-20260519T123456Z-zoe" / "network.har"


async def test_run_artifact_dir_defaults_to_utc_now(tmp_path: Path) -> None:
    """Constructing without ``run_id`` uses the current UTC timestamp."""
    artifact = RunArtifactDir(base=tmp_path, account_name="cat")
    run_dir = await asyncio.to_thread(artifact.ensure)
    assert _RUN_DIR_RE.match(run_dir.name)
    assert run_dir.name.endswith("-cat")


async def test_dump_screenshot_writes_real_png(har_context: BrowserContext, tmp_path: Path) -> None:
    """``dump_screenshot`` produces a real PNG with non-zero size."""
    page = await har_context.new_page()
    await page.set_content("<html><body><h1>screenshot fixture</h1></body></html>")
    target = tmp_path / "shot.png"
    written = await dump_screenshot(page, target)
    assert written == target
    assert target.is_file()
    size = await asyncio.to_thread(lambda: target.stat().st_size)
    assert size > 0
    head = await asyncio.to_thread(lambda: target.read_bytes()[:8])
    # PNG magic bytes.
    assert head == b"\x89PNG\r\n\x1a\n"


async def test_dump_dom_writes_rendered_html(har_context: BrowserContext, tmp_path: Path) -> None:
    """``dump_dom`` writes ``page.content()`` to disk."""
    page = await har_context.new_page()
    await page.set_content("<html><body><h1 id='hdr'>dom fixture body</h1></body></html>")
    target = tmp_path / "dom.html"
    written = await dump_dom(page, target)
    assert written == target
    assert target.is_file()
    text = await asyncio.to_thread(lambda: target.read_text(encoding="utf-8"))
    assert "dom fixture body" in text
    assert target.stat().st_size > 0


async def test_dump_console_writes_buffered_messages(tmp_path: Path) -> None:
    """``dump_console`` writes a non-empty file even with no messages."""
    target = tmp_path / "console.log"
    written = await dump_console(
        [("log", "hello world"), ("warn", "uh oh")],
        target,
    )
    assert written == target
    text = await asyncio.to_thread(lambda: target.read_text(encoding="utf-8"))
    assert "[log] hello world" in text
    assert "[warn] uh oh" in text
    assert target.stat().st_size > 0


async def test_dump_console_writes_placeholder_when_empty(tmp_path: Path) -> None:
    """An empty message list still produces a non-empty file."""
    target = tmp_path / "console.log"
    await dump_console([], target)
    text = await asyncio.to_thread(lambda: target.read_text(encoding="utf-8"))
    assert text.strip() != ""
    assert target.stat().st_size > 0


async def test_start_har_returns_context_with_har_recording(
    chromium: Browser, tmp_path: Path
) -> None:
    """``start_har()`` creates a context configured to record HAR to ``file``."""
    from src.utils.run_artifacts import start_har, stop_har

    target = tmp_path / "network.har"
    context = await start_har(chromium, target)
    try:
        page = await context.new_page()
        await page.goto("about:blank")
    finally:
        await stop_har(context)

    assert target.is_file()
    size = await asyncio.to_thread(lambda: target.stat().st_size)
    assert size > 0
    # HAR is JSON — parse and confirm the canonical top-level "log" key.
    parsed = await asyncio.to_thread(lambda: json.loads(target.read_text(encoding="utf-8")))
    assert "log" in parsed
    assert isinstance(parsed["log"], dict)


async def test_failure_dump_produces_all_four_artifacts(
    har_context: BrowserContext, tmp_path: Path
) -> None:
    """End-to-end: navigate to about:blank, force a failure, dump everything.

    Verifies the validation contract assertion ``io.run-artifacts-complete``:
    all four files exist under ``logs/run-<UTC>-<account>/`` with size > 0.
    """
    fixed_now = datetime(2026, 5, 19, 12, 34, 56, tzinfo=timezone.utc)
    base = tmp_path / "logs"
    artifact = RunArtifactDir(base=base, run_id=fixed_now, account_name="alice")

    # Buffer console messages exactly as the production hook would.
    console_buffer: list[tuple[str, str]] = []

    def _on_console(msg: ConsoleMessage) -> None:
        console_buffer.append((msg.type, msg.text))

    har_context.on("console", _on_console)

    page = await har_context.new_page()
    await page.goto("about:blank")
    await page.evaluate(
        "() => { console.log('forced-failure-marker'); console.warn('also-warned'); }"
    )
    # Console events are delivered async over CDP — yield so they land in
    # ``console_buffer`` before we read it.
    await asyncio.sleep(0.1)

    # Force a "failure" — write all four artefacts.
    run_dir = await asyncio.to_thread(artifact.ensure)
    screenshot_path = await dump_screenshot(page, artifact.path("screenshot.png"))
    dom_path = await dump_dom(page, artifact.path("dom.html"))
    console_path = await dump_console(console_buffer, artifact.path("console.log"))

    # HAR is only finalised on ``context.close()``. Closing the context
    # is the production-correct moment to do this (the runner closes the
    # context in its ``finally`` block).
    har_path = artifact.path("network.har")
    # Move the HAR file into the run dir as part of "stop": the context
    # was created with ``record_har_path=tmp_path/'_pre_close_har.har'``;
    # we close and then relocate. (Production wires the HAR path
    # directly into the context at creation time so no move is needed —
    # the helper supports both flows.)
    await har_context.close()

    def _relocate_har(src_glob_root: Path, dest: Path) -> None:
        candidate = next(src_glob_root.glob("_pre_close_har.har"))
        candidate.replace(dest)

    await asyncio.to_thread(_relocate_har, tmp_path, har_path)

    # Assert: directory + every file exists with non-zero size.
    assert run_dir == base / "run-20260519T123456Z-alice"
    assert run_dir.is_dir()

    sizes = await asyncio.to_thread(
        lambda: {
            "screenshot": screenshot_path.stat().st_size,
            "dom": dom_path.stat().st_size,
            "console": console_path.stat().st_size,
            "har": har_path.stat().st_size,
        }
    )
    assert sizes["screenshot"] > 0
    assert sizes["dom"] > 0
    assert sizes["console"] > 0
    assert sizes["har"] > 0

    # Spot-check artifact contents — confirms they are not zero-byte placeholders.
    head = await asyncio.to_thread(lambda: screenshot_path.read_bytes()[:8])
    assert head == b"\x89PNG\r\n\x1a\n"
    dom_text = await asyncio.to_thread(lambda: dom_path.read_text(encoding="utf-8"))
    assert "<html" in dom_text.lower()
    console_text = await asyncio.to_thread(lambda: console_path.read_text(encoding="utf-8"))
    assert "forced-failure-marker" in console_text
    har_text = await asyncio.to_thread(lambda: har_path.read_text(encoding="utf-8"))
    assert '"log"' in har_text  # HAR canonical top-level key
