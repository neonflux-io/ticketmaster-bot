"""Real-network + payload-shape tests for :class:`SlackNotifier`.

The "live" test POSTs to the user-configured ``SLACK_WEBHOOK_URL`` and
asserts a ``2xx`` response with Slack's documented success body ``"ok"``.
When ``SLACK_WEBHOOK_URL`` is not configured the test skips cleanly via
``pytest.skip``, satisfying the [io.slack-deferred] mission contract:
ship real, ready-to-run code but never silently pass without doing the
work.

The remaining tests point the notifier at a real local FastAPI server
booted on an ephemeral port via ``uvicorn.Server``; the server captures
the request body so we can assert the Slack-shaped payload contract:

* ``Content-Type: application/json``
* JSON body carries top-level ``text`` and ``blocks`` keys
* ``blocks[0]`` is a ``section`` block whose ``text`` is a ``mrkdwn``
  text element wrapping the title in ``*bold*``
* Slack's "200 + plain text != 'ok'" failure mode is detected by the
  client and converted to :class:`NotifierError` (NOT retried)
* The inherited :class:`WebhookNotifier` 5xx retry loop still drives the
  Slack subclass correctly (503/503/200+"ok" → exactly 3 POSTs).
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
from fastapi.responses import PlainTextResponse, Response

from src.notifiers.base import NotifierError, NotifyEvent
from src.notifiers.slack import (
    MRKDWN_ESCAPES,
    SLACK_OK_BODY,
    SlackNotifier,
    escape_mrkdwn,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _slack_webhook_url() -> str | None:
    """Load ``.env`` (best-effort) and return the configured webhook URL."""
    load_dotenv(override=False)
    url = os.getenv("SLACK_WEBHOOK_URL")
    if url:
        url = url.strip()
    return url or None


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _SequenceServer:
    """FastAPI app that returns a pre-programmed sequence of Slack responses.

    Each POST consumes one ``(status, body)`` pair from ``responses``;
    subsequent requests beyond the sequence keep returning the final
    pair so callers can decide whether to assert on extra hits. A status
    of 200 with body ``"ok"`` mimics Slack's documented success; other
    bodies at 200 mimic Slack's "200 + plain text error" failure mode.
    """

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self._remaining = list(responses)
        self._last = responses[-1] if responses else (200, "ok")
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
            status, text = self._remaining.pop(0) if self._remaining else self._last
            return PlainTextResponse(content=text, status_code=status)

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
# [io.slack-deferred] - real live POST when SLACK_WEBHOOK_URL is set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_real_post_returns_ok() -> None:
    """POST a real ``NotifyEvent`` to ``SLACK_WEBHOOK_URL`` and confirm "ok"."""
    url = _slack_webhook_url()
    if not url:
        pytest.skip("SLACK_WEBHOOK_URL not configured")

    notifier = SlackNotifier(webhook_url=url)
    event = NotifyEvent(
        event_type="mission_readiness",
        title="SlackNotifier live check",
        message="POSTed from tests/notifiers/test_slack_live.py",
        severity="info",
        metadata={"source": "pytest", "feature": "F3.5"},
    )

    await notifier.notify(event)

    response = notifier.last_response
    assert response is not None, "notifier should record the final response"
    assert response.status_code < 300, (
        f"expected 2xx, got {response.status_code}: {response.text[:200]!r}"
    )
    assert response.text.strip() == SLACK_OK_BODY, (
        f"expected Slack ack body 'ok', got {response.text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Slack-shaped payload contract (Block Kit section / mrkdwn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_payload_shape_against_local_server() -> None:
    """Build and POST a payload through real httpx to a real local server,
    then assert Slack Block Kit shape contract on the captured request body.
    """
    server = _SequenceServer([(200, "ok")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
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
    assert headers.get("content-type") == "application/json"

    payload = json.loads(body)
    assert isinstance(payload, dict)
    assert "text" in payload
    assert "blocks" in payload

    text = payload["text"]
    assert isinstance(text, str)
    assert "Cart ready" in text
    assert "2x Section 100 reserved" in text

    blocks = payload["blocks"]
    assert isinstance(blocks, list) and len(blocks) >= 1
    section = blocks[0]
    assert isinstance(section, dict)
    assert section["type"] == "section"
    block_text = section["text"]
    assert isinstance(block_text, dict)
    assert block_text["type"] == "mrkdwn"
    rendered = block_text["text"]
    assert isinstance(rendered, str)
    # Title is wrapped in Slack mrkdwn bold markers; message follows on a
    # new line.
    assert rendered.startswith("*Cart ready*")
    assert "\n" in rendered
    assert "2x Section 100 reserved" in rendered


# ---------------------------------------------------------------------------
# "200 + plain text != 'ok'" failure mode - the whole reason this class
# overrides notify().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_raises_when_body_is_not_ok_even_on_200() -> None:
    """Slack returns ``200`` + plain text on misuse; that must NOT pass."""
    server = _SequenceServer([(200, "invalid_payload")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError) as info:
            await notifier.notify(_make_event())

    # Slack failures are terminal - one POST, no retries.
    assert server.hits == 1, (
        f"non-'ok' body must not be retried; expected 1 POST, got {server.hits}"
    )
    msg = str(info.value)
    assert "invalid_payload" in msg
    assert "ok" in msg


@pytest.mark.asyncio
async def test_slack_accepts_ok_with_surrounding_whitespace() -> None:
    """Slack's success body is the literal string ``ok``; whitespace tolerated."""
    server = _SequenceServer([(200, "ok\n")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    assert server.hits == 1
    response = notifier.last_response
    assert response is not None
    assert response.status_code == 200
    assert response.text.strip() == SLACK_OK_BODY


@pytest.mark.asyncio
async def test_slack_rejects_uppercase_ok_body() -> None:
    """Body comparison is case-sensitive; Slack only ever sends lowercase."""
    server = _SequenceServer([(200, "OK")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError):
            await notifier.notify(_make_event())

    assert server.hits == 1


# ---------------------------------------------------------------------------
# Retry parity with WebhookNotifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_retries_on_5xx_then_succeeds() -> None:
    """503, 503, 200+"ok" → exactly 3 POSTs, third one succeeds."""
    server = _SequenceServer([(503, "boom"), (503, "boom"), (200, "ok")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    response = notifier.last_response
    assert response is not None
    assert response.status_code == 200
    assert response.text.strip() == SLACK_OK_BODY
    assert server.hits == 3


@pytest.mark.asyncio
async def test_slack_does_not_retry_on_4xx() -> None:
    """A 400 response (e.g. bad URL) must not be retried."""
    server = _SequenceServer([(400, "no_service"), (400, "no_service"), (400, "no_service")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError):
            await notifier.notify(_make_event())

    assert server.hits == 1


@pytest.mark.asyncio
async def test_slack_raises_after_exhausting_5xx_retries() -> None:
    """503 / 503 / 503 → 3 POSTs and finally :class:`NotifierError`."""
    server = _SequenceServer([(503, "boom"), (503, "boom"), (503, "boom")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError):
            await notifier.notify(_make_event())

    assert server.hits == 3


# ---------------------------------------------------------------------------
# mrkdwn escaping helper
# ---------------------------------------------------------------------------


def test_escape_mrkdwn_escapes_documented_specials() -> None:
    """Only ``&``, ``<``, ``>`` are HTML-escaped; nothing else changes."""
    expected = {"&", "<", ">"}
    assert {pair[0] for pair in MRKDWN_ESCAPES} == expected
    assert escape_mrkdwn("&") == "&amp;"
    assert escape_mrkdwn("<a>") == "&lt;a&gt;"
    # Slack formatting markers stay untouched so users can still bold/italic.
    assert escape_mrkdwn("*bold* _italic_ ~strike~ `code`") == (
        "*bold* _italic_ ~strike~ `code`"
    )


def test_escape_mrkdwn_does_not_double_escape_ampersands() -> None:
    """The ``&`` -> ``&amp;`` rewrite runs first, so injected entities are safe."""
    out = escape_mrkdwn("<a>&<b>")
    assert out == "&lt;a&gt;&amp;&lt;b&gt;"


@pytest.mark.asyncio
async def test_slack_escapes_mrkdwn_specials_in_title_and_message() -> None:
    """``&``, ``<``, ``>`` in user content arrive HTML-escaped in the block text."""
    server = _SequenceServer([(200, "ok")])

    async with _serve(server) as base:
        notifier = SlackNotifier(
            webhook_url=f"{base}/hook",
            timeout=5.0,
            backoff_base=0.01,
        )
        event = NotifyEvent(
            event_type="cart_success",
            title="Q&A <update>",
            message="see <https://example.com|here> & ack",
            severity="info",
        )
        await notifier.notify(event)

    assert server.hits == 1
    payload = json.loads(server.requests[0]["body"])  # type: ignore[arg-type]
    block_text = payload["blocks"][0]["text"]["text"]
    # Title is bolded; specials inside it are HTML-escaped.
    assert block_text.startswith("*Q&amp;A &lt;update&gt;*")
    # Message body is HTML-escaped too - Slack link tokens like
    # <url|label> must NOT be passed through verbatim, since user content
    # may collide with that syntax.
    assert "&lt;https://example.com|here&gt;" in block_text
    assert "&amp; ack" in block_text


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_slack_notifier_registered() -> None:
    from src.registry import notifiers as notifier_registry

    cls = notifier_registry.get("slack")
    assert cls is SlackNotifier
