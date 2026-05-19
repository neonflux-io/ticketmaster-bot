"""Tests for :mod:`src.observability.metrics`.

Covers the validation contract assertion ``io.prometheus-metrics-endpoint``:
the metrics exporter binds to a free port discovered via
``socket.bind(("127.0.0.1", 0))``, and ``httpx.get(f"http://127.0.0.1:{port}/metrics")``
returns status ``200`` with ``Content-Type`` starting with ``text/plain`` and
body containing the substring ``ticketmaster_bot_``.

Tests boot a real :mod:`uvicorn` server in a background asyncio task and tear
it down via ``server.should_exit = True`` so no orphan listener survives.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from src.observability.metrics import (
    RUN_DURATION_SECONDS,
    RUNS_FAILED_TOTAL,
    RUNS_SUCCEEDED_TOTAL,
    RUNS_TOTAL,
    make_asgi_app,
    start_metrics_server,
)


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_until_listening(port: int, timeout: float = 5.0) -> None:
    """Poll the metrics endpoint until it answers or ``timeout`` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        last_error: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                response = await client.get(f"http://127.0.0.1:{port}/metrics")
                if response.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last_error = exc
            await asyncio.sleep(0.05)
        raise RuntimeError(f"Metrics server never answered on port {port}: {last_error}")


def test_counters_and_histogram_are_defined() -> None:
    """The four metrics required by the feature description exist."""
    # _total suffix is appended automatically by prometheus_client; the
    # base names below correspond to the suffixed names in the exposition
    # format.
    assert RUNS_TOTAL._name == "ticketmaster_bot_runs"
    assert RUNS_SUCCEEDED_TOTAL._name == "ticketmaster_bot_runs_succeeded"
    assert RUNS_FAILED_TOTAL._name == "ticketmaster_bot_runs_failed"
    assert RUN_DURATION_SECONDS._name == "ticketmaster_bot_run_duration_seconds"


def test_make_asgi_app_returns_callable() -> None:
    """``make_asgi_app()`` returns an ASGI application (a 3-arg callable)."""
    app = make_asgi_app()
    assert callable(app)


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_text() -> None:
    """Boot the server on a free port and assert the /metrics contract."""
    port = _free_port()
    server = await start_metrics_server(port)
    try:
        await _wait_until_listening(port)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/metrics")
        assert response.status_code == 200
        content_type = response.headers.get("content-type", "")
        assert content_type.startswith("text/plain"), content_type
        body = response.text
        assert "ticketmaster_bot_runs_total" in body
        # Sanity-check the rest of the metric set is exposed.
        assert "ticketmaster_bot_runs_succeeded_total" in body
        assert "ticketmaster_bot_runs_failed_total" in body
        assert "ticketmaster_bot_run_duration_seconds" in body
    finally:
        server.should_exit = True
        # Wait for the background task to wind down so no listener leaks.
        await asyncio.wait_for(server.serve_task, timeout=5.0)

    # Port must be released cleanly.
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_counter_increment_reflected_in_endpoint() -> None:
    """Incrementing the runs counter changes the value in the /metrics output."""
    port = _free_port()
    server = await start_metrics_server(port)
    try:
        await _wait_until_listening(port)
        RUNS_TOTAL.inc()
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/metrics")
        assert response.status_code == 200
        body = response.text
        # The counter line is rendered as `name value` (whitespace separated).
        # We do not assert an absolute value (other tests may also tick the
        # counter when the test order is shuffled); we only require that the
        # counter line exists with a numeric value >= 1.
        for line in body.splitlines():
            if line.startswith("ticketmaster_bot_runs_total "):
                value = float(line.split()[-1])
                assert value >= 1
                break
        else:  # pragma: no cover - defensive
            raise AssertionError("ticketmaster_bot_runs_total line not found in body")
    finally:
        server.should_exit = True
        await asyncio.wait_for(server.serve_task, timeout=5.0)
