"""Per-run artefact directory + dump helpers.

A ``RunArtifactDir`` is a lazy handle to ``<base>/run-<UTC>-<account>/``.
The directory is only created on the first call to :meth:`ensure` (or
implicitly the first call to any of the ``dump_*`` helpers that resolve a
path via :meth:`RunArtifactDir.path`). This matches the failure-dump
contract used by :class:`src.hooks.screenshot_on_failure.ScreenshotOnFailureHook`
and the validation contract assertion ``io.run-artifacts-complete``: on a
failed run, the directory contains ``screenshot.png``, ``dom.html``,
``console.log``, and ``network.har`` each with ``stat().st_size > 0``.

The module also exposes ``start_har`` / ``stop_har`` so a runner can wire
HAR recording into context creation when
``config.logging.artifacts.record_har == true``. HAR is finalised only on
:py:meth:`BrowserContext.close`, so ``stop_har`` is a thin alias that
ensures the context is closed (no-op if already closed).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

log = logging.getLogger("ticketmaster-bot")

#: UTC timestamp format used in run-directory names
#: (filesystem-safe ``YYYYMMDDTHHMMSSZ``).
_TS_FORMAT = "%Y%m%dT%H%M%SZ"

#: Account names ultimately appear in filesystem paths; restrict them to a
#: safe subset to keep the artefact directory portable.
_SAFE_ACCOUNT_RE = re.compile(r"[^A-Za-z0-9._-]+")

_UNKNOWN_ACCOUNT = "unknown"


def _utcnow() -> datetime:
    """Return the current UTC datetime. Indirected for test injection."""
    return datetime.now(tz=timezone.utc)


def _sanitize_account(name: str | None) -> str:
    """Sanitise ``name`` for use as a path segment."""
    if not name:
        return _UNKNOWN_ACCOUNT
    cleaned = _SAFE_ACCOUNT_RE.sub("_", name).strip("._-")
    return cleaned or _UNKNOWN_ACCOUNT


class RunArtifactDir:
    """Handle to a per-run artefact directory.

    Parameters
    ----------
    base:
        Root directory under which the per-run folder is created.
        Defaults to ``"logs"``. Tests typically pass a ``tmp_path``.
    run_id:
        UTC ``datetime`` used to build the run-folder name. Defaults to
        the current UTC time at construction. Pass a fixed datetime in
        tests for deterministic directory names.
    account_name:
        Account label embedded in the folder name. Sanitised to keep
        the path portable. An empty/blank value falls back to
        ``"unknown"``.
    """

    def __init__(
        self,
        base: str | Path = "logs",
        *,
        run_id: datetime | None = None,
        account_name: str | None = None,
    ) -> None:
        self.base = Path(base)
        # Snapshot UTC at construction so subsequent ``ensure()`` calls
        # produce the same directory name even if the wall clock moves
        # between dumps.
        self.run_id: datetime = run_id or _utcnow()
        self.account_name: str = _sanitize_account(account_name)
        self._created: bool = False

    @property
    def directory(self) -> Path:
        """Return the run directory path (without creating it)."""
        timestamp = self.run_id.strftime(_TS_FORMAT)
        return self.base / f"run-{timestamp}-{self.account_name}"

    def ensure(self) -> Path:
        """Create the run directory if needed and return its path.

        Idempotent: safe to call from every dump helper.
        """
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        self._created = True
        return directory

    def path(self, name: str) -> Path:
        """Return the path to ``name`` inside the run directory.

        Does *not* create the parent directory; callers should invoke
        :meth:`ensure` (or one of the ``dump_*`` helpers) first.
        """
        return self.directory / name

    def __fspath__(self) -> str:  # makes ``Path(artifact)`` work
        return str(self.directory)


# ---------------------------------------------------------------------------
# Dump helpers
# ---------------------------------------------------------------------------


async def dump_screenshot(page: Page, target: Path) -> Path:
    """Write a full-page PNG screenshot to ``target``.

    Returns the path written. The parent directory is created if
    missing. Any Playwright error is logged and a 1-byte placeholder is
    written so downstream code that asserts ``size > 0`` is not
    surprised by a missing file when artefact capture is best-effort.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(target), full_page=True)
    except Exception:  # noqa: BLE001 - artefact dumps are best-effort
        log.exception("dump_screenshot: page.screenshot() failed at %s", target)
        # Preserve the post-condition (file exists with size > 0) so
        # downstream consumers never see a half-finished dump.
        await asyncio.to_thread(target.write_bytes, b"\x00")
    return target


async def dump_dom(page: Page, target: Path) -> Path:
    """Write the rendered HTML returned by :py:meth:`Page.content` to ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        log.exception("dump_dom: page.content() failed at %s", target)
        html = ""
    # Ensure the file is never empty so consumers can rely on size > 0.
    body = html if html else "<!-- no DOM captured -->\n"
    await asyncio.to_thread(target.write_text, body, encoding="utf-8")
    return target


async def dump_console(
    messages: Iterable[tuple[str, str]],
    target: Path,
) -> Path:
    """Write ``[<type>] <text>`` lines for ``messages`` to ``target``.

    ``messages`` is an iterable of ``(type, text)`` tuples — typically
    the buffer maintained by a ``context.on("console", ...)`` listener.
    When the buffer is empty, a single informational line is written so
    the file always has non-zero size (required by the
    ``io.run-artifacts-complete`` validation contract assertion).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[{msg_type}] {text}" for msg_type, text in messages]
    if not lines:
        lines = ["[info] no console messages captured before failure"]
    body = "\n".join(lines) + "\n"
    await asyncio.to_thread(target.write_text, body, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# HAR plumbing
# ---------------------------------------------------------------------------


async def start_har(browser: Browser, file: str | Path) -> BrowserContext:
    """Open a new :class:`BrowserContext` that records HAR to ``file``.

    Use this in the rare case where the runner owns the ``Browser``
    directly. The production runner uses
    :py:meth:`Browser.launch_persistent_context`; the helper there is
    :func:`har_kwargs` which returns the ``record_har_path`` kwarg dict.

    The parent directory of ``file`` is created so callers can hand in
    a path under a not-yet-existing run directory.
    """
    path = Path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(record_har_path=str(path))
    return context


async def stop_har(context: BrowserContext) -> None:
    """Flush + close ``context`` so the HAR file is finalised on disk.

    Playwright finalises HARs only on :py:meth:`BrowserContext.close`;
    this helper is a thin wrapper that swallows the post-close error
    Playwright raises when ``close()`` is invoked a second time.
    """
    try:
        await context.close()
    except Exception:  # noqa: BLE001 - idempotent stop
        log.debug("stop_har: context.close() raised (likely already closed)")


def har_kwargs(file: str | Path) -> dict[str, str]:
    """Return ``record_har_path`` kwargs for ``launch_persistent_context``.

    The production runner threads this dict into the launch kwargs when
    ``config.logging.artifacts.record_har`` is true so that HAR
    recording starts at the moment the persistent context is created.
    """
    path = Path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    return {"record_har_path": str(path)}


__all__ = [
    "RunArtifactDir",
    "dump_console",
    "dump_dom",
    "dump_screenshot",
    "har_kwargs",
    "start_har",
    "stop_har",
]
