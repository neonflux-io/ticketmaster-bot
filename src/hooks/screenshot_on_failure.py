"""Hook that dumps a screenshot, DOM, and console log when a run fails.

The hook attaches a context-level ``console`` listener on the first event
it sees that carries a Playwright ``BrowserContext`` (typically
``on_run_start`` from :class:`~src.vendors.ticketmaster.core.BotRunner`,
which forwards ``context=`` once it has launched Chromium). Console
messages are buffered in memory until the ``on_failure`` event fires, at
which point the hook writes three artifacts to
``<base_dir>/run-<UTC>-<account>/``:

* ``screenshot.png`` - a full-page PNG captured via :py:meth:`Page.screenshot`.
* ``dom.html`` - the rendered HTML returned by :py:meth:`Page.content`.
* ``console.log`` - one console message per line, formatted as
  ``[<type>] <text>``.

The hook is deliberately defensive: any I/O or Playwright failure during
the dump is logged and swallowed so the run's own failure handler isn't
masked by an artefact-collection problem.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import Hook

log = logging.getLogger("ticketmaster-bot")

#: Default base directory for run artifacts. Resolved relative to the
#: current working directory so tests can override via ``base_dir=``.
DEFAULT_BASE_DIR = "logs"

#: UTC timestamp format used in run-directory names. Matches the spec in
#: the feature description (``logs/run-<UTC>-<account>/``) using a
#: filesystem-safe layout ``YYYYMMDDTHHMMSSZ``.
_TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Account names ultimately appear in filesystem paths; restrict them to a
# safe subset to keep the artefact directory portable.
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


class ScreenshotOnFailureHook(Hook):
    """Dump screenshot.png / dom.html / console.log on ``on_failure``.

    Parameters
    ----------
    base_dir:
        Directory under which per-run artifact folders are created.
        Defaults to ``"logs"``. Tests pass a ``tmp_path`` so dumps go to
        a temporary directory.
    clock:
        Optional callable returning the current UTC datetime. Injected
        by tests that need a deterministic run-directory name. Defaults
        to :func:`_utcnow`.
    """

    # Failure dumps are observational; let other hooks (e.g. slow-down)
    # fire first by giving the screenshot hook a positive priority.
    priority: int = 50

    def __init__(
        self,
        base_dir: str | Path = DEFAULT_BASE_DIR,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self._clock: Callable[[], datetime] = clock or _utcnow
        # Buffered console messages keyed by the context whose ``console``
        # event produced them. Keyed by ``id(context)`` so we never hold
        # a hard reference to the Playwright context (which would prevent
        # garbage collection if a hook outlives its owning runner).
        self._console: dict[int, list[str]] = {}
        # Listener callables retained per context so we can detach in a
        # ``finally`` after the dump completes.
        self._listeners: dict[int, Callable[[Any], None]] = {}
        # Bound context references kept only for unsubscription; the
        # dictionary is cleared as soon as the dump finishes.
        self._contexts: dict[int, Any] = {}

    # ------------------------------------------------------------------
    # Hook protocol
    # ------------------------------------------------------------------

    async def on_event(self, ctx: Any, event_type: str, **kwargs: Any) -> None:
        # ``context`` may arrive on any event that the runner emits once
        # Chromium is live. Attach the listener lazily so the hook works
        # regardless of which event the runner forwards it on.
        browser_context = kwargs.get("context")
        if browser_context is not None:
            self._ensure_listener(browser_context)

        if event_type != "on_failure":
            return

        page = kwargs.get("page")
        account = self._resolve_account(ctx, kwargs)
        try:
            await self._dump(ctx=ctx, page=page, account=account)
        except Exception:  # noqa: BLE001 - artefact dump must never re-raise
            log.exception(
                "ScreenshotOnFailureHook: failed to write artefacts for account=%s",
                account,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_listener(self, browser_context: Any) -> None:
        key = id(browser_context)
        if key in self._listeners:
            return
        buffer: list[str] = []

        def _on_console(msg: Any) -> None:
            try:
                # Playwright's ConsoleMessage exposes .type and .text. Be
                # defensive: a future API change shouldn't break the
                # whole hook, just degrade the formatting.
                msg_type = getattr(msg, "type", None) or "log"
                text = getattr(msg, "text", None) or str(msg)
            except Exception:  # noqa: BLE001
                msg_type, text = "log", str(msg)
            buffer.append(f"[{msg_type}] {text}")

        try:
            browser_context.on("console", _on_console)
        except Exception:  # noqa: BLE001
            log.exception("ScreenshotOnFailureHook: could not subscribe to context console events")
            return
        self._console[key] = buffer
        self._listeners[key] = _on_console
        self._contexts[key] = browser_context

    def _detach_listeners(self) -> None:
        for key, context in list(self._contexts.items()):
            listener = self._listeners.get(key)
            if listener is None:
                continue
            try:
                context.remove_listener("console", listener)
            except Exception:  # noqa: BLE001
                # Older Playwright versions may not expose
                # ``remove_listener`` on BrowserContext - swallow and
                # move on; the context is about to be closed anyway.
                log.debug("ScreenshotOnFailureHook: remove_listener failed; continuing")
        self._listeners.clear()
        self._contexts.clear()

    def _resolve_account(self, ctx: Any, kwargs: dict[str, Any]) -> str:
        """Resolve the account name to embed in the artifact directory."""
        explicit = kwargs.get("account")
        if explicit:
            return str(explicit)
        # BotRunner exposes ``self.account`` (an ``AccountConfig`` with
        # ``.name``); accept that conventionally without taking a hard
        # dependency on the type.
        if ctx is not None:
            account_obj = getattr(ctx, "account", None)
            if account_obj is not None:
                name = getattr(account_obj, "name", None)
                if name:
                    return str(name)
        return _UNKNOWN_ACCOUNT

    def _run_dir(self, account: str) -> Path:
        timestamp = self._clock().strftime(_TS_FORMAT)
        safe = _sanitize_account(account)
        return self.base_dir / f"run-{timestamp}-{safe}"

    async def _dump(self, *, ctx: Any, page: Any, account: str) -> None:
        if page is None:
            log.warning(
                "ScreenshotOnFailureHook: no page available; skipping artefact dump for account=%s",
                account,
            )
            return

        run_dir = self._run_dir(account)
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("ScreenshotOnFailureHook: could not create run dir %s", run_dir)
            return

        screenshot_path = run_dir / "screenshot.png"
        dom_path = run_dir / "dom.html"
        console_path = run_dir / "console.log"

        try:
            await self._dump_screenshot(page, screenshot_path)
            await self._dump_dom(page, dom_path)
            self._dump_console(console_path)
            log.info(
                "ScreenshotOnFailureHook: wrote artefacts to %s",
                run_dir,
            )
        finally:
            self._detach_listeners()

    async def _dump_screenshot(self, page: Any, path: Path) -> None:
        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception:  # noqa: BLE001
            log.exception("ScreenshotOnFailureHook: page.screenshot() failed at %s", path)
            # Ensure the file exists so downstream consumers don't trip on
            # a missing path; write a placeholder byte instead.
            await asyncio.to_thread(path.write_bytes, b"\x00")

    async def _dump_dom(self, page: Any, path: Path) -> None:
        try:
            content = await page.content()
        except Exception:  # noqa: BLE001
            log.exception("ScreenshotOnFailureHook: page.content() failed at %s", path)
            content = ""
        await asyncio.to_thread(path.write_text, content or "", encoding="utf-8")

    def _dump_console(self, path: Path) -> None:
        lines: list[str] = []
        for buffer in self._console.values():
            lines.extend(buffer)
        # Even when no console messages were captured, write a single
        # informational line so the file always has non-zero size and
        # downstream tools have something to display. The validation
        # contract requires ``stat().st_size > 0``.
        if not lines:
            lines = ["[info] no console messages captured before failure"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = ["DEFAULT_BASE_DIR", "ScreenshotOnFailureHook"]
