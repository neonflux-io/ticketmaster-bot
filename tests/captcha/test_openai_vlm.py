"""Tests for :class:`OpenAIVLMSolver` in ``src/captcha/providers/openai_vlm.py``.

Every HTTP-touching test points the solver at a real local FastAPI server
booted on an ephemeral port via ``uvicorn.Server.serve()`` - no
``unittest.mock``, no ``httpx.MockTransport``, no monkeypatching of
network calls. The server captures every inbound request so the tests can
assert on the exact payload shape that hit the wire.

The response bodies the local server returns are real JSON shaped to
match OpenAI's chat-completions vision endpoint as documented at
``https://platform.openai.com/docs/api-reference/chat/create``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from src.captcha import CaptchaChallenge, CaptchaSolution
from src.captcha.providers.openai_vlm import OpenAIVLMSolver

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to port 0 and return the kernel-assigned port number."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _png_bytes() -> bytes:
    """Return tiny but valid PNG bytes (1x1 transparent pixel)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _make_challenge(screenshot: bytes | None = None) -> CaptchaChallenge:
    return CaptchaChallenge(
        challenge_type="yii_image",
        screenshot_bytes=screenshot if screenshot is not None else _png_bytes(),
        refresh_callable=None,
        input_locator=None,
        page=None,
    )


class _OpenAIServer:
    """Real FastAPI app that mimics OpenAI's chat-completions vision response.

    ``responses`` is an ordered list of values that drive each POST in turn:

    * ``str`` -> a 200 with that string as the assistant content.
    * ``int`` -> an HTTP status code with an empty body (for errors).
    * ``dict`` -> a 200 whose JSON body is exactly that dict (used to test
      empty/missing choices arrays etc.).

    Each captured request is appended to :attr:`requests` so tests can
    introspect headers and body.
    """

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.app = FastAPI()

        @self.app.post("/v1/chat/completions")
        async def chat_completions(request: Request) -> Response:
            body = await request.body()
            self.requests.append(
                {
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "body": body,
                }
            )
            if not self._responses:
                # Default to 200/empty if test under-provisions.
                return JSONResponse({"choices": []})
            spec = self._responses.pop(0)
            if isinstance(spec, int):
                return Response(status_code=spec)
            if isinstance(spec, dict):
                return JSONResponse(spec)
            # Treat as a string assistant content.
            return JSONResponse(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": str(spec)},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

    @property
    def hits(self) -> int:
        return len(self.requests)


@asynccontextmanager
async def _serve(server: _OpenAIServer) -> AsyncIterator[str]:
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
        # Hit a never-defined route to confirm socket is accepting.
        for _ in range(50):
            try:
                await probe.get(base + "/__probe__")
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)

    try:
        yield base
    finally:
        uvi.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Construction + env handling
# ---------------------------------------------------------------------------


def test_construct_with_explicit_args() -> None:
    solver = OpenAIVLMSolver(
        api_key="sk-explicit",
        model="explicit-model",
        base_url="http://example/v1/chat/completions",
        timeout_s=12.5,
        max_chars=5,
    )
    assert solver.name == "openai_vlm"
    assert solver.api_key == "sk-explicit"
    assert solver.model == "explicit-model"
    assert solver.base_url == "http://example/v1/chat/completions"
    assert solver.timeout_s == 12.5
    assert solver.max_chars == 5


def test_supports_returns_yii_image() -> None:
    assert OpenAIVLMSolver.supports() == {"yii_image"}


def test_construct_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-123")
    monkeypatch.setenv("OPENAI_CAPTCHA_MODEL", "gpt-from-env")
    solver = OpenAIVLMSolver()
    assert solver.api_key == "sk-from-env-123"
    assert solver.model == "gpt-from-env"


def test_construct_default_base_url() -> None:
    solver = OpenAIVLMSolver(api_key="sk-x", model="m")
    assert solver.base_url == "https://api.openai.com/v1/chat/completions"


def test_raises_value_error_when_model_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When neither the ``model`` arg nor ``OPENAI_CAPTCHA_MODEL`` is set, raise."""
    monkeypatch.delenv("OPENAI_CAPTCHA_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    with pytest.raises(ValueError, match="OPENAI_CAPTCHA_MODEL"):
        OpenAIVLMSolver()


def test_explicit_model_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_CAPTCHA_MODEL", "should-not-win")
    solver = OpenAIVLMSolver(api_key="sk-x", model="explicit-wins")
    assert solver.model == "explicit-wins"


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


async def test_payload_shape_includes_model_image_and_prompt() -> None:
    server = _OpenAIServer(["ABCDE"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk-test-key",
            model="gpt-test-vision",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        png = _png_bytes()
        solution = await solver.solve(_make_challenge(png))

    assert solution is not None
    assert server.hits == 1
    request = server.requests[0]
    headers = request["headers"]
    body = request["body"]
    assert isinstance(headers, dict)
    assert isinstance(body, (bytes, bytearray))

    # Required headers.
    assert headers["content-type"].startswith("application/json")
    assert headers["authorization"] == "Bearer sk-test-key"

    payload = json.loads(body)
    assert payload["model"] == "gpt-test-vision"
    assert isinstance(payload["messages"], list) and len(payload["messages"]) >= 1

    user_msg = payload["messages"][0]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, list)

    # One text block (the prompt) + one image block.
    text_blocks = [c for c in content if c.get("type") == "text"]
    image_blocks = [c for c in content if c.get("type") == "image_url"]
    assert len(text_blocks) == 1
    assert len(image_blocks) == 1

    prompt_text = text_blocks[0]["text"]
    # The prompt instructs the model to extract the captcha text only.
    lowered = prompt_text.lower()
    assert "captcha" in lowered or "text" in lowered
    # No-punctuation / no-explanation instruction.
    assert "only" in lowered

    # Image is a data URL with base64-encoded PNG bytes.
    image_url = image_blocks[0]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    encoded = image_url.split(",", 1)[1]
    assert base64.b64decode(encoded) == png


async def test_payload_max_tokens_bounded() -> None:
    """The request asks for a bounded number of tokens so the model doesn't ramble."""
    server = _OpenAIServer(["XYZ"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        await solver.solve(_make_challenge())
    payload = json.loads(server.requests[0]["body"])  # type: ignore[arg-type]
    assert "max_tokens" in payload or "max_completion_tokens" in payload


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


async def test_solve_happy_path_returns_solution() -> None:
    server = _OpenAIServer(["abcde"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())

    assert isinstance(solution, CaptchaSolution)
    # Answer normalised to uppercase.
    assert solution.text == "ABCDE"
    assert solution.confidence is None
    assert solution.provider == "openai_vlm"
    assert solution.latency_ms >= 0


async def test_solve_strips_whitespace_in_response() -> None:
    server = _OpenAIServer(["  AB12  \n"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is not None
    assert solution.text == "AB12"


async def test_solve_accepts_four_char_alphanumeric_answer() -> None:
    server = _OpenAIServer(["ab12"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is not None
    assert solution.text == "AB12"


async def test_solve_rejects_answer_too_long() -> None:
    server = _OpenAIServer(["ABCDEFGHIJ"])  # 10 chars
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_rejects_answer_with_punctuation() -> None:
    server = _OpenAIServer(["A!B@C"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_rejects_empty_response() -> None:
    server = _OpenAIServer([""])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_rejects_too_short_answer() -> None:
    """``"AB"`` is 2 chars - below the 3-char floor of the regex."""
    server = _OpenAIServer(["AB"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_returns_none_on_api_error_status() -> None:
    server = _OpenAIServer([500])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None
    assert server.hits == 1


async def test_solve_returns_none_on_4xx() -> None:
    server = _OpenAIServer([401])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_returns_none_on_missing_choices() -> None:
    server = _OpenAIServer([{"id": "x", "object": "chat.completion", "choices": []}])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_returns_none_on_malformed_response() -> None:
    server = _OpenAIServer([{"unexpected": "schema"}])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_returns_none_on_unreachable_server() -> None:
    """A closed local port simulates a network error / DNS failure."""
    closed_port = _free_port()
    solver = OpenAIVLMSolver(
        api_key="sk",
        model="m",
        base_url=f"http://127.0.0.1:{closed_port}/v1/chat/completions",
        timeout_s=1.0,
    )
    solution = await solver.solve(_make_challenge())
    assert solution is None


async def test_solve_returns_none_on_timeout() -> None:
    """A server that sleeps past ``timeout_s`` causes the solver to give up."""

    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def slow(_: Request) -> Response:
        await asyncio.sleep(2.0)
        return JSONResponse({"choices": [{"message": {"content": "ABCD"}}]})

    port = _free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
        access_log=False,
    )
    uvi = uvicorn.Server(config)
    task = asyncio.create_task(uvi.serve())
    try:
        for _ in range(50):
            if uvi.started:
                break
            await asyncio.sleep(0.05)
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="m",
            base_url=f"http://127.0.0.1:{port}/v1/chat/completions",
            timeout_s=0.5,
        )
        solution = await solver.solve(_make_challenge())
        assert solution is None
    finally:
        uvi.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


async def test_solve_logs_api_call_with_model_and_latency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _OpenAIServer(["WXYZ"])
    async with _serve(server) as base:
        solver = OpenAIVLMSolver(
            api_key="sk",
            model="gpt-log-test",
            base_url=f"{base}/v1/chat/completions",
            timeout_s=5.0,
        )
        with caplog.at_level(logging.DEBUG, logger="ticketmaster-bot"):
            await solver.solve(_make_challenge())

    messages = "\n".join(r.getMessage() for r in caplog.records if r.name == "ticketmaster-bot")
    assert "gpt-log-test" in messages
    # Latency mentioned in some form ("ms" or "latency").
    assert "ms" in messages.lower() or "latency" in messages.lower()


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_openai_vlm_registered_under_name() -> None:
    """Importing ``src.captcha`` should register the solver under ``"openai_vlm"``."""
    from src.captcha import registry as captcha_registry

    cls_or_obj = captcha_registry.get("openai_vlm")
    # Either the class itself or an instance is acceptable; both are real
    # implementations of CaptchaSolver registered by the package.
    if isinstance(cls_or_obj, type):
        assert issubclass(cls_or_obj, OpenAIVLMSolver) or cls_or_obj is OpenAIVLMSolver
    else:
        assert isinstance(cls_or_obj, OpenAIVLMSolver)


# ---------------------------------------------------------------------------
# Sanity: real env (.env) defaults shape (no live call)
# ---------------------------------------------------------------------------


def test_env_defaults_present_in_dotenv() -> None:
    """Sanity check: the committed ``.env`` carries an OPENAI_CAPTCHA_MODEL line.

    This is a static lint - we read the file, look for the key. The actual
    value is the user's choice and is not asserted (the model identifier
    will likely change over time). Skips cleanly if ``.env`` is absent.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    env_path = os.path.join(repo_root, ".env")
    if not os.path.isfile(env_path):
        pytest.skip(".env not present on this checkout")
    with open(env_path, encoding="utf-8") as f:
        content = f.read()
    assert "OPENAI_API_KEY=" in content
    assert "OPENAI_CAPTCHA_MODEL=" in content
