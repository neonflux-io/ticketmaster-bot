"""Retry-policy tests for :class:`WebhookNotifier`.

Drives the notifier's retry loop against a real local FastAPI app booted on
an ephemeral port via ``uvicorn.Server``. No mocks, no monkeypatching of the
HTTP layer - every assertion is grounded in real socket traffic captured by
the test server.

Covers:
* ``[io.notifier-retry-5xx]`` - 503, 503, 200 produces exactly 3 POSTs and
  the third returns 200.
* ``[io.notifier-no-retry-4xx]`` - a 400 response produces exactly 1 POST
  and raises :class:`NotifierError`.
* ``[io.webhook-payload-shape]`` - the body the server receives parses as a
  JSON dict carrying at least ``event``, ``title``, ``message``,
  ``severity``, ``timestamp_iso`` and ``metadata`` keys.
* ``[io.webhook-real-post]`` ``Content-Type`` guarantee - the server sees
  ``application/json`` on each request.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

from src.notifiers.base import NotifierError, NotifyEvent
from src.notifiers.webhook import WebhookNotifier


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _SequenceServer:
    """FastAPI app that returns a pre-programmed sequence of status codes.

    Each POST consumes one status code from ``responses``; subsequent
    requests beyond the sequence keep returning the final code so callers
    can decide whether to assert on extra hits.
    """

    def __init__(self, responses: list[int]) -> None:
        self._remaining = list(responses)
        self._last = responses[-1] if responses else 200
        self.requests: list[dict[str, object]] = []
        self.app = FastAPI()

        @self.app.post("/hook")
        async def hook(request: Request) -> Response:
            body = await request.body()
            self.requests.append(
                {
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "body": body,
                }
            )
            status = self._remaining.pop(0) if self._remaining else self._last
            return Response(status_code=status)

    @property
    def hits(self) -> int:
        return len(self.requests)


@asynccontextmanager
async def _serve(server: _SequenceServer) -> AsyncIterator[str]:
    """Boot ``server`` on an ephemeral port and yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(
        app=server.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    uvi = uvicorn.Server(config)
    task = asyncio.create_task(uvi.serve())

    # Wait until uvicorn binds the socket.
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=2.0) as probe:
        for _ in range(50):
            if uvi.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive
            raise RuntimeError("uvicorn never started")
        # Also confirm reachable at the socket level.
        for _ in range(50):
            try:
                await probe.get(base + "/__nope__")
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)

    try:
        yield base
    finally:
        uvi.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


def _make_event() -> NotifyEvent:
    return NotifyEvent(
        event_type="cart_success",
        title="Cart ready",
        message="Tickets reserved",
        severity="info",
        metadata={"account": "alice", "section": "100"},
    )


# ---------------------------------------------------------------------------
# [io.notifier-retry-5xx]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_retries_5xx_until_success() -> None:
    server = _SequenceServer([503, 503, 200])

    async with _serve(server) as base:
        notifier = WebhookNotifier(
            url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    response = notifier.last_response
    assert response is not None
    assert response.status_code == 200
    assert server.hits == 3, f"expected exactly 3 POSTs (503, 503, 200), got {server.hits}"
    # All requests must declare JSON content type.
    for entry in server.requests:
        headers = entry["headers"]
        assert isinstance(headers, dict)
        assert headers.get("content-type") == "application/json"


# ---------------------------------------------------------------------------
# [io.notifier-no-retry-4xx]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_does_not_retry_on_4xx() -> None:
    server = _SequenceServer([400, 400, 400])

    async with _serve(server) as base:
        notifier = WebhookNotifier(
            url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError) as info:
            await notifier.notify(_make_event())

    assert server.hits == 1, f"4xx must not retry; expected 1 POST, got {server.hits}"
    assert "400" in str(info.value)


@pytest.mark.asyncio
async def test_webhook_raises_after_exhausting_5xx_retries() -> None:
    server = _SequenceServer([503, 503, 503])

    async with _serve(server) as base:
        notifier = WebhookNotifier(
            url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError):
            await notifier.notify(_make_event())

    assert server.hits == 3


# ---------------------------------------------------------------------------
# [io.webhook-payload-shape]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_payload_shape_and_headers() -> None:
    server = _SequenceServer([200])

    async with _serve(server) as base:
        notifier = WebhookNotifier(
            url=f"{base}/hook",
            headers={"X-Test": "webhook-payload"},
            timeout=5.0,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    assert server.hits == 1
    request = server.requests[0]
    headers = request["headers"]
    body = request["body"]
    assert isinstance(headers, dict)
    assert isinstance(body, (bytes, bytearray))
    # Content-Type and custom headers both made it through.
    assert headers.get("content-type") == "application/json"
    assert headers.get("x-test") == "webhook-payload"

    payload = json.loads(body)
    assert isinstance(payload, dict)
    for key in ("event", "title", "message", "severity", "timestamp_iso", "metadata"):
        assert key in payload, f"missing {key!r} in webhook payload"
    assert payload["event"] == "cart_success"
    assert payload["title"] == "Cart ready"
    assert payload["message"] == "Tickets reserved"
    assert payload["severity"] == "info"
    assert isinstance(payload["timestamp_iso"], str) and payload["timestamp_iso"]
    assert payload["metadata"] == {"account": "alice", "section": "100"}


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_webhook_notifier_registered() -> None:
    from src.registry import notifiers as notifier_registry

    cls = notifier_registry.get("webhook")
    assert cls is WebhookNotifier
