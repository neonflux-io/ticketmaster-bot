"""Real-network + payload-shape tests for :class:`TelegramNotifier`.

The "live" test POSTs to ``https://api.telegram.org/bot<TOKEN>/sendMessage``
using credentials sourced from the gitignored ``.env`` and asserts the
Bot API's standard ``{"ok": true, ...}`` JSON body. It ``pytest.skip``s
cleanly when either env var is missing, satisfying the mission rule that
we never silently pass without doing the work.

The remaining tests point the notifier at a real local FastAPI server
booted on an ephemeral port via ``uvicorn.Server``; the server captures
the request body so we can assert the Telegram-shaped payload contract:

* the request URL contains ``/bot<token>/sendMessage``
* the JSON body carries ``chat_id``, ``text``, and ``parse_mode``
* ``parse_mode`` is exactly ``"MarkdownV2"``
* the rendered ``text`` is ``"*<title>*\\n\\n<message>"`` with every
  MarkdownV2 special character escaped via the notifier's helper

A retry test reuses the local server scaffold to confirm
:class:`WebhookNotifier`'s 5xx → exponential-backoff loop still drives
the Telegram subclass correctly (503/503/200 → exactly 3 POSTs, third
returns 200 with the ``{"ok": true}`` body).
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
from fastapi.responses import JSONResponse, Response

from src.notifiers.base import NotifierError, NotifyEvent
from src.notifiers.telegram import (
    MARKDOWN_V2_SPECIAL_CHARS,
    TelegramNotifier,
    escape_markdown_v2,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _telegram_creds() -> tuple[str | None, str | None]:
    """Load ``.env`` (best-effort) and return ``(bot_token, chat_id)``."""
    load_dotenv(override=False)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if token:
        token = token.strip()
    if chat:
        chat = chat.strip()
    return (token or None, chat or None)


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _SequenceServer:
    """FastAPI app that returns a pre-programmed sequence of status codes.

    Each POST consumes one status code from ``responses``; subsequent
    requests beyond the sequence keep returning the final code so callers
    can decide whether to assert on extra hits. When a ``200`` is returned,
    the server emits a Telegram-shaped ``{"ok": true, ...}`` JSON body so
    that ``response.json()["ok"]`` assertions work end-to-end.
    """

    def __init__(self, responses: list[int]) -> None:
        self._remaining = list(responses)
        self._last = responses[-1] if responses else 200
        self.requests: list[dict[str, object]] = []
        self.app = FastAPI()

        @self.app.post("/bot{token_path:path}/sendMessage")
        async def send_message(token_path: str, request: Request) -> Response:
            body = await request.body()
            self.requests.append(
                {
                    "token_path": token_path,
                    "path": str(request.url.path),
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "body": body,
                }
            )
            status = self._remaining.pop(0) if self._remaining else self._last
            if status == 200:
                return JSONResponse(
                    {
                        "ok": True,
                        "result": {
                            "message_id": len(self.requests),
                            "chat": {"id": 0},
                            "text": "ack",
                        },
                    }
                )
            if 400 <= status < 500:
                return JSONResponse(
                    {"ok": False, "error_code": status, "description": "client error"},
                    status_code=status,
                )
            # 5xx returns plain text so we exercise the non-JSON path too.
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
# [io.telegram-real-post]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_real_post_returns_ok_true() -> None:
    """POST a real ``NotifyEvent`` to api.telegram.org and assert ``ok=True``."""
    token, chat_id = _telegram_creds()
    if not token or not chat_id:
        pytest.skip("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured")

    notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
    event = NotifyEvent(
        event_type="mission_readiness",
        title="TelegramNotifier live check",
        message="POSTed from tests/notifiers/test_telegram_live.py",
        severity="info",
        metadata={"source": "pytest", "feature": "F3.4"},
    )

    await notifier.notify(event)

    response = notifier.last_response
    assert response is not None, "notifier should record the final response"
    assert response.status_code < 300, (
        f"expected 2xx, got {response.status_code}: {response.text[:200]!r}"
    )
    body = response.json()
    assert isinstance(body, dict)
    assert body.get("ok") is True, f"expected ok=true, got {body!r}"


# ---------------------------------------------------------------------------
# [io.telegram-payload-shape]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_payload_shape_against_local_server() -> None:
    """Build and POST a payload through real httpx to a real local server,
    then assert Telegram-shape contract on the captured request body.
    """
    server = _SequenceServer([200])

    async with _serve(server) as base:
        notifier = TelegramNotifier(
            bot_token="123:ABC",
            chat_id="42",
            api_base=base,
            timeout=5.0,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    assert server.hits == 1
    request = server.requests[0]
    # The path includes the bot token; assert the canonical form.
    assert request["path"] == "/bot123:ABC/sendMessage"
    headers = request["headers"]
    body = request["body"]
    assert isinstance(headers, dict)
    assert isinstance(body, (bytes, bytearray))
    assert headers.get("content-type") == "application/json"

    payload = json.loads(body)
    assert isinstance(payload, dict)
    assert set(payload).issuperset({"chat_id", "text", "parse_mode"})
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "MarkdownV2"
    text = payload["text"]
    assert isinstance(text, str)
    # Body is "*<title>*\n\n<message>" with content escaped for MarkdownV2.
    assert text.startswith("*Cart ready*")
    assert "\n\n" in text
    # The message contains "100" which is harmless, but the literal "."
    # in nothing-special text is still safe; the helper only escapes the
    # documented MarkdownV2 special chars.
    assert "Section 100 reserved" in text


# ---------------------------------------------------------------------------
# MarkdownV2 escaping helper
# ---------------------------------------------------------------------------


def test_escape_markdown_v2_escapes_every_special_char() -> None:
    """Every Telegram MarkdownV2 special char becomes its escaped form."""
    # Reserved chars per Bot API docs:
    # _ * [ ] ( ) ~ ` > # + - = | { } . !
    expected = {
        "_",
        "*",
        "[",
        "]",
        "(",
        ")",
        "~",
        "`",
        ">",
        "#",
        "+",
        "-",
        "=",
        "|",
        "{",
        "}",
        ".",
        "!",
    }
    assert set(MARKDOWN_V2_SPECIAL_CHARS) == expected
    for ch in MARKDOWN_V2_SPECIAL_CHARS:
        assert escape_markdown_v2(ch) == "\\" + ch, f"{ch!r} not escaped correctly"


def test_escape_markdown_v2_leaves_plain_text_untouched() -> None:
    assert escape_markdown_v2("Hello world 123") == "Hello world 123"
    # Backslashes themselves are also escaped so existing literal "\" don't
    # accidentally form escape sequences.
    assert escape_markdown_v2("line\\break") == "line\\\\break"


def test_escape_markdown_v2_mixed_string() -> None:
    src = "Section 100-200 (resale) - $50.00!"
    out = escape_markdown_v2(src)
    # Every reserved char shows up backslash-prefixed.
    assert "\\-" in out
    assert "\\(" in out and "\\)" in out
    assert "\\." in out
    assert "\\!" in out
    # Untouched chars still appear.
    assert "Section 100" in out


@pytest.mark.asyncio
async def test_telegram_escapes_special_chars_in_title_and_message() -> None:
    """Reserved chars in title/message arrive backslash-escaped in body.text."""
    server = _SequenceServer([200])

    async with _serve(server) as base:
        notifier = TelegramNotifier(
            bot_token="123:ABC",
            chat_id="42",
            api_base=base,
            timeout=5.0,
            backoff_base=0.01,
        )
        event = NotifyEvent(
            event_type="cart_success",
            title="Section 100-200 (resale)",
            message="Price: $50.50! Hold #1.",
            severity="info",
        )
        await notifier.notify(event)

    assert server.hits == 1
    payload = json.loads(server.requests[0]["body"])  # type: ignore[arg-type]
    text = payload["text"]
    # The literal Telegram-bold markers wrapping the title MUST NOT be
    # escaped - they are formatting, not user content.
    assert text.startswith("*Section 100\\-200 \\(resale\\)*")
    # Body separator is literal "\n\n".
    assert "\n\n" in text
    # Message-content reserved chars are escaped. ``$`` is NOT in the
    # MarkdownV2 reserved set so it passes through verbatim.
    assert "Price: $50\\.50\\!" in text
    assert "Hold \\#1\\." in text


# ---------------------------------------------------------------------------
# Retry parity with WebhookNotifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_retries_on_5xx_then_succeeds() -> None:
    """503, 503, 200 → exactly 3 POSTs, third one succeeds with ok=true."""
    server = _SequenceServer([503, 503, 200])

    async with _serve(server) as base:
        notifier = TelegramNotifier(
            bot_token="123:ABC",
            chat_id="42",
            api_base=base,
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        await notifier.notify(_make_event())

    response = notifier.last_response
    assert response is not None
    assert response.status_code == 200
    assert server.hits == 3
    assert response.json()["ok"] is True


@pytest.mark.asyncio
async def test_telegram_does_not_retry_on_4xx() -> None:
    """A 400 response (e.g. bad chat_id) must not be retried."""
    server = _SequenceServer([400, 400, 400])

    async with _serve(server) as base:
        notifier = TelegramNotifier(
            bot_token="123:ABC",
            chat_id="42",
            api_base=base,
            timeout=5.0,
            max_attempts=3,
            backoff_base=0.01,
        )
        with pytest.raises(NotifierError):
            await notifier.notify(_make_event())

    assert server.hits == 1


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_telegram_requires_token_and_chat_id() -> None:
    with pytest.raises(ValueError):
        TelegramNotifier(bot_token="", chat_id="42")
    with pytest.raises(ValueError):
        TelegramNotifier(bot_token="123:ABC", chat_id="")


def test_telegram_url_built_from_token_and_api_base() -> None:
    n = TelegramNotifier(bot_token="123:ABC", chat_id="42")
    assert n.url == "https://api.telegram.org/bot123:ABC/sendMessage"
    n2 = TelegramNotifier(
        bot_token="987:XYZ",
        chat_id="42",
        api_base="https://example.test",
    )
    assert n2.url == "https://example.test/bot987:XYZ/sendMessage"


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_telegram_notifier_registered() -> None:
    from src.registry import notifiers as notifier_registry

    cls = notifier_registry.get("telegram")
    assert cls is TelegramNotifier
