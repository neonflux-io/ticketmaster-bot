"""FastAPI control panel for the running bot.

The control panel binds to ``host:port`` and exposes three endpoints over
HTTP:

* ``GET /status`` - returns a JSON object describing the runner's current
  state, the active account name, the event URL being driven, and the
  ``started_at`` timestamp (ISO 8601).
* ``GET /logs/tail?n=100`` - returns the last ``n`` lines from the log
  file the runner is currently writing to. ``n`` defaults to ``100`` and
  is clamped to a non-negative integer.
* ``POST /stop`` - sets the runner's shared shutdown
  :class:`asyncio.Event` and flips :attr:`uvicorn.Server.should_exit` to
  ``True`` so the next loop iteration drains and exits. Responds ``202
  Accepted``.

The server is hosted by :class:`uvicorn.Server` + :meth:`uvicorn.Server.serve`
running in a background :class:`asyncio.Task`, mirroring
:func:`src.observability.metrics.start_metrics_server`. Tests boot the
server on an ephemeral port (``socket.bind(("127.0.0.1", 0))``) and tear
it down via the same ``serve_task`` handle so no orphan listener
survives.

This module covers the validation-contract assertions
``io.control-status-200``, ``io.control-stop-shuts-down``, and
``io.servers-no-orphan-listeners``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI


@dataclass
class RunnerRef:
    """Observable snapshot of an in-flight runner.

    The :class:`ControlServer` reads from this object on every request;
    the runner (or its supervising coordinator) writes to it as state
    transitions occur. Keeping it a plain dataclass means tests can
    construct a minimal instance without needing to instantiate the
    full ``BotRunner`` (which would pull in Playwright and a config
    tree).

    Attributes
    ----------
    state:
        Free-form short label for the current lifecycle phase
        (``"idle"``, ``"running"``, ``"queue"``, ``"checkout"``, ...).
    account:
        Name of the account currently being driven, or ``None`` if the
        runner has not bound to one yet.
    event_url:
        URL of the event currently being driven, or ``None`` if the
        runner has not opened one yet.
    started_at:
        UTC timestamp at which the runner started. Defaults to ``now``
        at construction so a freshly built ``RunnerRef`` already has a
        sensible value.
    log_file:
        Path to the log file the runner is writing to. The
        ``/logs/tail`` endpoint reads from this path. ``None`` (or a
        missing file) is handled gracefully -- the endpoint returns an
        empty list of lines in that case rather than raising.
    stop_event:
        Shared :class:`asyncio.Event` the runner monitors for shutdown.
        ``POST /stop`` sets this event. A fresh event is created per
        instance so each ``RunnerRef`` is independently observable.
    """

    state: str = "idle"
    account: str | None = None
    event_url: str | None = None
    started_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    log_file: Path | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


def _tail_lines(path: Path, n: int) -> list[str]:
    """Return the last ``n`` lines of ``path`` (or ``[]`` if missing).

    Uses a bounded :class:`collections.deque` so very large log files
    are streamed without holding the whole file in memory. Trailing
    newline characters are stripped from each returned line.
    """
    if n <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            tail: deque[str] = deque(fh, maxlen=n)
    except FileNotFoundError:
        return []
    return [line.rstrip("\n") for line in tail]


class ControlServer:
    """Background FastAPI control panel for a single in-flight runner.

    Parameters
    ----------
    port:
        TCP port to bind. Tests should pass an ephemeral port obtained
        by binding to ``0`` and reading back the kernel-assigned port.
    runner_ref:
        The :class:`RunnerRef` the server reads state from and writes a
        shutdown signal to. The control panel never mutates anything on
        the runner besides setting :attr:`RunnerRef.stop_event`.
    host:
        Interface to bind. Defaults to ``"127.0.0.1"`` so the panel is
        not exposed beyond the local machine.

    The server is constructed but not yet running. Call
    :meth:`serve` to schedule the background task.
    """

    def __init__(
        self,
        port: int,
        runner_ref: RunnerRef,
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self.port: int = int(port)
        self.host: str = host
        self.runner_ref: RunnerRef = runner_ref
        self._app: FastAPI = self._build_app()
        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="error",
            lifespan="off",
            access_log=False,
        )
        self._server: uvicorn.Server = uvicorn.Server(config)
        self.serve_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Server lifecycle.
    # ------------------------------------------------------------------

    async def serve(self) -> asyncio.Task[None]:
        """Schedule :meth:`uvicorn.Server.serve` as a background task.

        Idempotent: calling :meth:`serve` after the task has already
        been scheduled returns the same task.
        """
        if self.serve_task is None:
            self.serve_task = asyncio.create_task(
                self._server.serve(),
                name=f"control-server-{self.port}",
            )
        return self.serve_task

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Flip ``should_exit`` and wait for :attr:`serve_task` to finish.

        Safe to call regardless of whether the server has actually been
        started. If ``serve_task`` never came up the method returns
        immediately.
        """
        self._server.should_exit = True
        task = self.serve_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def should_exit(self) -> bool:
        """Whether uvicorn has been asked to shut down."""
        return bool(self._server.should_exit)

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._server.should_exit = bool(value)

    @property
    def started(self) -> bool:
        """Whether uvicorn has bound its listening socket."""
        return bool(self._server.started)

    @property
    def app(self) -> FastAPI:
        """The underlying FastAPI application (exposed for inspection)."""
        return self._app

    # ------------------------------------------------------------------
    # Routing.
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(
            title="ticketmaster-bot control panel",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        ref = self.runner_ref
        server = self  # captured for the /stop handler

        @app.get("/status")
        async def get_status() -> dict[str, Any]:
            started_at = ref.started_at
            return {
                "state": ref.state,
                "account": ref.account,
                "event_url": ref.event_url,
                "started_at": started_at.isoformat() if started_at is not None else None,
            }

        @app.get("/logs/tail")
        async def get_logs_tail(n: int = 100) -> dict[str, Any]:
            count = max(0, int(n))
            log_path = ref.log_file
            if log_path is None:
                return {
                    "lines": [],
                    "n": count,
                    "log_file": None,
                }
            path = Path(log_path)
            lines = _tail_lines(path, count)
            return {
                "lines": lines,
                "n": count,
                "log_file": str(path),
            }

        @app.post("/stop", status_code=202)
        async def post_stop() -> dict[str, str]:
            ref.stop_event.set()
            server._server.should_exit = True
            return {"status": "stopping"}

        return app


__all__ = [
    "ControlServer",
    "RunnerRef",
]
