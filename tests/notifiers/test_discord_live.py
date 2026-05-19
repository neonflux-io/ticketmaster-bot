"""Real-network + payload-shape tests for :class:`DiscordNotifier`.

The "live" test POSTs to the user-configured ``DISCORD_WEBHOOK_URL`` and
asserts a ``2xx`` response (Discord normally returns ``204 No Content``).
It ``pytest.skip``s cleanly when the env var is missing, satisfying the
mission rule that we never silently pass without doing the work.

The "payload shape" test points the notifier at a real local FastAPI
server booted on an ephemeral port via ``uvicorn.Server``. The server
captures the request body and headers; the test then asserts the
Discord-shaped payload contract ([io.discord-payload-shape]).

A bonus retry test reuses the same local server scaffold to confirm the
inherited :class:`WebhookNotifier` retry semantics still apply on the
Discord subclass: 503/503/200 → exactly 3 POSTs, third returns 200.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import Response

from src.notifiers.base import NotifyEvent
from src.notifiers.discord import (
    ERROR_COLOR,
    INFO_COLOR,
    SUCCESS_COLOR,
    DiscordNotifier,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _discord_webhook_url() -> str | None:
    """Load ``.env`` (best-effort) and return the configured webhook URL."""
    load_dotenv(override=False)
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if url:
        url = url.strip()
    return url or None


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

    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=2.0) as probe:
        for _ in range(50):
            if uvi.started:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive
            raise RuntimeError("uvicorn never started")
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


def _make_event(
    severity: str = "info",
    title: str = "Cart ready",
    message: str = "2x Section 100 reserved",
) -> NotifyEvent:
    return NotifyEvent(
        event_type="cart_success",
        title=title,
        message=message,
        severity=severity,
        metadata={"account": "alice", "section": "100"},
    )


# ---------------------------------------------------------------------------
# [io.discord-real-post]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_real_post_returns_2xx() -> None:
    """POST a real ``NotifyEvent`` to ``DISCORD_WEBHOOK_URL`` and confirm 2xx."""
    url = _discord_webhook_url()
    if not url:
        pytest.skip("DISCORD_WEBHOOK_URL not configured")

    notifier = DiscordNotifier(webhook_url=url)
    event = NotifyEvent(
        event_type="mission_readiness",
        title="DiscordNotifier live check",
        message="POSTed from tests/notifiers/test_discord_live.py",
        severity="info",
        metadata={"source": "pytest", "feature": "F3.3"},
    )

    await notifier.notify(event)

    response = notifier.last_response
    assert response is not None, "notifier should record the final response"
    assert response.status_code < 300, (
        f"expected 2xx, got {response.status_code}: {response.text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# [io.discord-payload-shape]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_payload_shape_against_local_server() -> None:
    """Build and POST a payload through real httpx to a real local server,
    then assert Discord-shape contract on the captured request body.
    """
    server = _SequenceServer([204])

    async with _serve(server) as base:
        notifier = DiscordNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event(severity="success"))

    assert server.hits == 1
    request = server.requests[0]
    headers = request["headers"]
    body = request["body"]
    assert isinstance(headers, dict)
    assert isinstance(body, (bytes, bytearray))
    assert headers.get("content-type") == "application/json"

    payload = json.loads(body)
    assert isinstance(payload, dict)
    # Either ``content`` OR ``embeds`` must carry the title/message; the
    # shipped notifier ships both.
    assert "content" in payload or "embeds" in payload
    assert "content" in payload
    assert "embeds" in payload

    content = payload["content"]
    assert isinstance(content, str)
    assert "Cart ready" in content
    assert "2x Section 100 reserved" in content

    embeds = payload["embeds"]
    assert isinstance(embeds, list) and len(embeds) >= 1
    embed = embeds[0]
    assert isinstance(embed, dict)
    assert embed["title"] == "Cart ready"
    assert embed["description"] == "2x Section 100 reserved"
    assert isinstance(embed["color"], int)
    assert embed["color"] == SUCCESS_COLOR  # severity="success" -> green
    assert isinstance(embed["timestamp"], str) and embed["timestamp"]

    # Optional metadata fields propagate when metadata is non-empty.
    assert "fields" in embed
    field_names = {f["name"] for f in embed["fields"]}
    assert {"account", "section"}.issubset(field_names)


@pytest.mark.asyncio
async def test_discord_severity_colour_map() -> None:
    """Each documented severity maps to the documented Discord colour int."""
    server = _SequenceServer([204, 204, 204])

    async with _serve(server) as base:
        notifier = DiscordNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event(severity="info"))
        await notifier.notify(_make_event(severity="success"))
        await notifier.notify(_make_event(severity="error"))

    assert server.hits == 3
    payloads = [json.loads(req["body"]) for req in server.requests]  # type: ignore[arg-type]
    colours = [p["embeds"][0]["color"] for p in payloads]
    assert colours == [INFO_COLOR, SUCCESS_COLOR, ERROR_COLOR]


# ---------------------------------------------------------------------------
# Retry parity with WebhookNotifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_retries_on_5xx_like_webhook() -> None:
    """503, 503, 200 → exactly 3 POSTs, third one succeeds."""
    server = _SequenceServer([503, 503, 200])

    async with _serve(server) as base:
        notifier = DiscordNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    response = notifier.last_response
    assert response is not None
    assert response.status_code == 200
    assert server.hits == 3


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_discord_notifier_registered() -> None:
    from src.registry import notifiers as notifier_registry

    cls = notifier_registry.get("discord")
    assert cls is DiscordNotifier
