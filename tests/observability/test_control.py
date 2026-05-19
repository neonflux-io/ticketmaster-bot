"""Tests for :mod:`src.observability.control`.

Covers validation-contract assertions:

* ``io.control-status-200`` -- ``GET /status`` returns ``200`` with a
  JSON body dict containing key ``state``.
* ``io.control-stop-shuts-down`` -- ``POST /stop`` returns ``2xx`` and
  within a few seconds the uvicorn server exits cleanly such that a
  follow-up ``GET /status`` raises :class:`httpx.ConnectError`.
* ``io.servers-no-orphan-listeners`` -- after teardown no process is
  bound to the test's chosen port (verified by re-binding to it).

Tests boot a real :mod:`uvicorn`-hosted FastAPI server on a free port,
exercise all three endpoints, then drive ``/stop`` and assert the next
request is refused. A subprocess-based ``lsof`` check guards against
orphan listeners surviving teardown.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
from pathlib import Path

import httpx
import pytest

from src.observability.control import ControlServer, RunnerRef


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_until_listening(port: int, path: str = "/status", timeout: float = 5.0) -> None:
    """Poll ``path`` until it answers ``200`` or ``timeout`` elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while loop.time() < deadline:
            try:
                response = await client.get(f"http://127.0.0.1:{port}{path}")
                if response.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last_error = exc
            await asyncio.sleep(0.05)
    raise RuntimeError(f"Control server never answered on port {port}: {last_error}")


async def _wait_until_refused(port: int, timeout: float = 5.0) -> Exception:
    """Poll ``/status`` until a connection error is raised; return that error."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    async with httpx.AsyncClient(timeout=1.0) as client:
        while loop.time() < deadline:
            try:
                await client.get(f"http://127.0.0.1:{port}/status")
            except httpx.ConnectError as exc:
                return exc
            await asyncio.sleep(0.05)
    raise RuntimeError(f"Control server still answering on port {port} after {timeout}s")


def _port_is_listening(port: int) -> bool:
    """Use ``lsof`` to check whether anything is LISTEN-bound to ``port``."""
    result = subprocess.run(
        ["lsof", "-iTCP:" + str(port), "-sTCP:LISTEN", "-P", "-n"],
        capture_output=True,
        text=True,
    )
    # lsof exits 1 when there are no matches; exit 0 + non-empty stdout
    # means at least one listener.
    return result.returncode == 0 and bool(result.stdout.strip())


@pytest.mark.asyncio
async def test_status_endpoint_returns_runner_snapshot(tmp_path: Path) -> None:
    """``GET /status`` returns the documented JSON shape and 200 status."""
    log_file = tmp_path / "bot.log"
    log_file.write_text("first line\nsecond line\nthird line\n", encoding="utf-8")
    ref = RunnerRef(
        state="running",
        account="alice",
        event_url="https://example.com/event/123",
        log_file=log_file,
    )
    expected_started_at = ref.started_at.isoformat()
    port = _free_port()
    server = ControlServer(port=port, runner_ref=ref)
    await server.serve()
    try:
        await _wait_until_listening(port)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/status")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, dict)
        # io.control-status-200: dict containing key "state".
        assert "state" in body
        assert body["state"] == "running"
        assert body["account"] == "alice"
        assert body["event_url"] == "https://example.com/event/123"
        assert body["started_at"] == expected_started_at
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_logs_tail_returns_last_n_lines(tmp_path: Path) -> None:
    """``GET /logs/tail?n=N`` returns the trailing ``N`` lines of the file."""
    log_file = tmp_path / "bot.log"
    log_file.write_text(
        "\n".join(f"line-{i}" for i in range(1, 11)) + "\n",
        encoding="utf-8",
    )
    ref = RunnerRef(log_file=log_file)
    port = _free_port()
    server = ControlServer(port=port, runner_ref=ref)
    await server.serve()
    try:
        await _wait_until_listening(port)
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Default (n=100) returns all 10 lines.
            default_response = await client.get(f"http://127.0.0.1:{port}/logs/tail")
            assert default_response.status_code == 200
            default_body = default_response.json()
            assert default_body["lines"] == [f"line-{i}" for i in range(1, 11)]
            assert default_body["n"] == 100

            # n=3 returns the last three lines only.
            small_response = await client.get(f"http://127.0.0.1:{port}/logs/tail?n=3")
            assert small_response.status_code == 200
            small_body = small_response.json()
            assert small_body["lines"] == ["line-8", "line-9", "line-10"]
            assert small_body["n"] == 3
            assert small_body["log_file"] == str(log_file)
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_logs_tail_handles_missing_log_file(tmp_path: Path) -> None:
    """A missing log file returns an empty ``lines`` list, not a 500."""
    ref = RunnerRef(log_file=tmp_path / "does-not-exist.log")
    port = _free_port()
    server = ControlServer(port=port, runner_ref=ref)
    await server.serve()
    try:
        await _wait_until_listening(port)
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/logs/tail?n=10")
        assert response.status_code == 200
        body = response.json()
        assert body["lines"] == []
        assert body["n"] == 10
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_stop_endpoint_shuts_server_down() -> None:
    """``POST /stop`` returns 202, sets the runner's event, and exits cleanly.

    After the server has exited, the next ``GET /status`` must raise
    :class:`httpx.ConnectError` (assertion ``io.control-stop-shuts-down``)
    and the listening port must be released (assertion
    ``io.servers-no-orphan-listeners``).
    """
    ref = RunnerRef(state="running")
    port = _free_port()
    server = ControlServer(port=port, runner_ref=ref)
    serve_task = await server.serve()
    try:
        await _wait_until_listening(port)
        assert _port_is_listening(port), "expected port to be listening before /stop"

        async with httpx.AsyncClient(timeout=5.0) as client:
            stop_response = await client.post(f"http://127.0.0.1:{port}/stop")
        assert stop_response.status_code == 202
        assert ref.stop_event.is_set()
        assert server.should_exit is True

        # uvicorn drains and exits; the serve task should resolve quickly.
        await asyncio.wait_for(serve_task, timeout=5.0)

        # Follow-up /status must now raise ConnectError.
        with pytest.raises(httpx.ConnectError):
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.get(f"http://127.0.0.1:{port}/status")

        # And the port must be re-bindable (no orphan listener).
        assert not _port_is_listening(port), (
            f"port {port} still has a listener after /stop"
        )
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
    finally:
        # shutdown is idempotent; safe even if the task already finished.
        await server.shutdown()


@pytest.mark.asyncio
async def test_no_orphan_listener_after_normal_shutdown() -> None:
    """After ``shutdown()`` returns, ``lsof`` shows no listener on the port."""
    ref = RunnerRef()
    port = _free_port()
    server = ControlServer(port=port, runner_ref=ref)
    await server.serve()
    await _wait_until_listening(port)
    assert _port_is_listening(port)

    await server.shutdown()
    assert server.serve_task is not None
    assert server.serve_task.done()
    assert not _port_is_listening(port), (
        f"port {port} still has a listener after shutdown()"
    )
    # And the port is immediately reusable.
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
