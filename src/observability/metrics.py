"""Prometheus metrics for the ticketmaster bot.

This module owns four Prometheus collectors:

* :data:`RUNS_TOTAL` — :class:`~prometheus_client.Counter` of every run
  attempted, exposed as ``ticketmaster_bot_runs_total``.
* :data:`RUNS_SUCCEEDED_TOTAL` — counter for runs that completed
  successfully, exposed as ``ticketmaster_bot_runs_succeeded_total``.
* :data:`RUNS_FAILED_TOTAL` — counter for runs that failed, exposed as
  ``ticketmaster_bot_runs_failed_total``.
* :data:`RUN_DURATION_SECONDS` — :class:`~prometheus_client.Histogram` of
  per-run wall-clock duration, exposed as
  ``ticketmaster_bot_run_duration_seconds``.

``prometheus_client`` automatically appends the ``_total`` suffix to
counter names in the exposition format, so the base names registered with
the library deliberately omit it.

:func:`make_asgi_app` returns the standard ``prometheus_client`` ASGI app
that serves the ``/metrics`` endpoint. :func:`start_metrics_server`
launches that app via :mod:`uvicorn` in a background :mod:`asyncio` task on
the requested port and returns a :class:`MetricsServer` handle whose
``should_exit`` flag drives a clean shutdown.

This module covers the validation contract assertion
``io.prometheus-metrics-endpoint``: the exporter binds to a free port,
``GET /metrics`` returns ``200`` with ``Content-Type`` starting with
``text/plain``, and the body contains ``ticketmaster_bot_runs_total``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import uvicorn
from prometheus_client import Counter, Histogram
from prometheus_client import make_asgi_app as _prom_make_asgi_app

#: Total runs attempted. Exposed as ``ticketmaster_bot_runs_total``.
RUNS_TOTAL: Counter = Counter(
    "ticketmaster_bot_runs",
    "Total bot runs attempted.",
)

#: Runs that completed successfully. Exposed as
#: ``ticketmaster_bot_runs_succeeded_total``.
RUNS_SUCCEEDED_TOTAL: Counter = Counter(
    "ticketmaster_bot_runs_succeeded",
    "Bot runs that completed successfully.",
)

#: Runs that failed. Exposed as ``ticketmaster_bot_runs_failed_total``.
RUNS_FAILED_TOTAL: Counter = Counter(
    "ticketmaster_bot_runs_failed",
    "Bot runs that failed.",
)

#: Per-run wall-clock duration in seconds.
RUN_DURATION_SECONDS: Histogram = Histogram(
    "ticketmaster_bot_run_duration_seconds",
    "Wall-clock duration of a single bot run, in seconds.",
)


def make_asgi_app() -> Any:
    """Return the ``prometheus_client`` ASGI application.

    The returned callable conforms to the ASGI 3.0 protocol and exposes
    ``/metrics`` over HTTP. It is suitable for mounting under any ASGI
    server, including :mod:`uvicorn` (used here) or as a sub-application
    in a FastAPI app.

    Typed as :class:`typing.Any` because :func:`prometheus_client.make_asgi_app`
    returns an untyped ASGI callable that ``uvicorn``'s structural protocol
    cannot narrow without an explicit cast.
    """
    return _prom_make_asgi_app()


class MetricsServer:
    """Handle to a background :mod:`uvicorn` server hosting ``/metrics``.

    Returned by :func:`start_metrics_server`. To stop the server, set
    :attr:`should_exit` to ``True`` and await :attr:`serve_task`; uvicorn
    drains in-flight connections and the task completes once the server
    has closed its listening socket.
    """

    def __init__(self, server: uvicorn.Server, serve_task: asyncio.Task[None]) -> None:
        self._server = server
        self.serve_task: asyncio.Task[None] = serve_task

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


async def start_metrics_server(port: int, host: str = "127.0.0.1") -> MetricsServer:
    """Start the Prometheus metrics ASGI app on ``host:port`` in the background.

    Parameters
    ----------
    port:
        TCP port to bind. Use ``0`` (or a kernel-assigned free port via
        :func:`socket.socket.bind`) in tests.
    host:
        Interface to bind. Defaults to ``127.0.0.1`` so the endpoint is
        not exposed beyond the local machine.

    Returns
    -------
    MetricsServer
        Handle exposing :attr:`MetricsServer.serve_task` (the background
        :class:`asyncio.Task`) and a :attr:`MetricsServer.should_exit`
        flag callers flip to trigger clean shutdown.
    """
    config = uvicorn.Config(
        app=make_asgi_app(),
        host=host,
        port=int(port),
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    server = uvicorn.Server(config)
    serve_task: asyncio.Task[None] = asyncio.create_task(
        server.serve(),
        name=f"metrics-server-{port}",
    )
    return MetricsServer(server, serve_task)


__all__ = [
    "RUNS_TOTAL",
    "RUNS_SUCCEEDED_TOTAL",
    "RUNS_FAILED_TOTAL",
    "RUN_DURATION_SECONDS",
    "MetricsServer",
    "make_asgi_app",
    "start_metrics_server",
]
