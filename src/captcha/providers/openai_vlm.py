"""OpenAI vision-language-model captcha solver.

:class:`OpenAIVLMSolver` sends the captured captcha image to OpenAI's
chat-completions endpoint (the same one that backs the ``gpt-*`` vision
models) and parses the model's plain-text reply as the captcha answer. It
satisfies the :class:`~src.captcha.base.CaptchaSolver` contract for
``challenge_type="yii_image"`` - the Yii-Framework alphanumeric image
captcha is the Ticketmaster Asia variant the bot encounters most often.

The request schema follows the public chat-completions reference:

* ``model``           - the OpenAI vision-capable model identifier.
* ``messages``        - a single ``role="user"`` message whose ``content``
  is a list with one ``"type": "text"`` block (the instruction prompt) and
  one ``"type": "image_url"`` block whose ``url`` is a base64-encoded PNG
  data URL.
* ``max_tokens``      - capped low (the answer is at most a handful of chars).
* ``temperature``     - 0 to make the response deterministic when the model
  recognises the characters cleanly.

The response is parsed defensively: anything other than a well-formed
``choices[0].message.content`` matching the ``^[A-Za-z0-9]{3,8}$`` regex
(stripped + uppercased) is treated as a failure and the solver returns
``None``. Network errors, HTTP errors, timeouts, and empty responses are
all surfaced the same way - the chain decides whether to retry on its
own. Each call is logged with the model name + latency so the operator
can spot a misbehaving provider.

Construction reads ``OPENAI_API_KEY`` and ``OPENAI_CAPTCHA_MODEL`` from
the process environment when the corresponding keyword argument is not
passed. ``OPENAI_CAPTCHA_MODEL`` has **no default**: omitting it (and
omitting the ``model`` argument) is a configuration error that raises
``ValueError`` immediately so we never silently send requests to an
unspecified model.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from typing import Any

import httpx

from ..base import CaptchaChallenge, CaptchaSolution, CaptchaSolver

log = logging.getLogger("ticketmaster-bot")

#: Default OpenAI chat-completions endpoint. Callers may override via the
#: ``base_url`` constructor argument (used by tests that point at a local
#: FastAPI server bound to an ephemeral port).
DEFAULT_BASE_URL = "https://api.openai.com/v1/chat/completions"

#: The prompt instructs the model to emit *only* the recognised characters,
#: no punctuation, no explanation. Keeping it tight reduces parsing failures.
_PROMPT = (
    "You are an OCR engine specialising in distorted alphanumeric captchas. "
    "The image below is a Yii-Framework captcha containing 4 or 5 letters and digits. "
    "Read the captcha and respond with ONLY the characters - no spaces, "
    "no punctuation, no explanation, no quotes. Output the bare text only."
)

#: Allowed answer pattern. Yii captchas are 4-5 chars alphanumeric; we
#: accept 3-8 for resilience against minor mis-recognitions and other
#: captcha variants whose width drifts a bit.
_ANSWER_RE = re.compile(r"^[A-Za-z0-9]{3,8}$")


class OpenAIVLMSolver(CaptchaSolver):
    """Solve a ``yii_image`` captcha by asking an OpenAI vision model."""

    name = "openai_vlm"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_chars: int = 5,
    ) -> None:
        resolved_api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not resolved_api_key:
            # An empty key is still a valid construction state - the solver
            # will simply fail on the first request with a 401. Surfacing a
            # ValueError here would prevent registering the solver class at
            # import time when the env var is absent. Log a warning so the
            # operator sees the misconfiguration.
            log.warning(
                "OpenAIVLMSolver constructed without an API key "
                "(OPENAI_API_KEY env var unset); calls will fail authentication."
            )

        resolved_model = model if model is not None else os.environ.get("OPENAI_CAPTCHA_MODEL")
        if not resolved_model:
            raise ValueError(
                "OpenAIVLMSolver requires a model. Pass model=... explicitly "
                "or set the OPENAI_CAPTCHA_MODEL environment variable."
            )

        self.api_key: str = resolved_api_key
        self.model: str = resolved_model
        self.base_url: str = base_url if base_url is not None else DEFAULT_BASE_URL
        self.timeout_s: float = float(timeout_s)
        self.max_chars: int = int(max_chars)

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image"}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        """Return a :class:`CaptchaSolution` or ``None`` on any failure."""
        payload = self._build_payload(challenge.screenshot_bytes)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.timeout_s, connect=min(self.timeout_s, 5.0))

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.base_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            log.warning(
                "openai_vlm call model=%s latency=%dms outcome=timeout error=%s",
                self.model,
                latency_ms,
                exc,
            )
            return None
        except httpx.HTTPError as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            log.warning(
                "openai_vlm call model=%s latency=%dms outcome=network_error error=%s",
                self.model,
                latency_ms,
                exc,
            )
            return None

        latency_ms = int((time.perf_counter() - start) * 1000)

        if response.status_code >= 400:
            log.warning(
                "openai_vlm call model=%s latency=%dms outcome=http_error status=%d body=%r",
                self.model,
                latency_ms,
                response.status_code,
                response.text[:200],
            )
            return None

        try:
            data = response.json()
        except ValueError:
            log.warning(
                "openai_vlm call model=%s latency=%dms outcome=invalid_json body=%r",
                self.model,
                latency_ms,
                response.text[:200],
            )
            return None

        answer = self._extract_answer(data)
        if answer is None:
            log.info(
                "openai_vlm call model=%s latency=%dms outcome=no_answer raw=%r",
                self.model,
                latency_ms,
                data,
            )
            return None

        log.info(
            "openai_vlm call model=%s latency=%dms outcome=success answer_len=%d",
            self.model,
            latency_ms,
            len(answer),
        )
        return CaptchaSolution(
            text=answer.upper(),
            confidence=None,
            provider=self.name,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _build_payload(self, screenshot_bytes: bytes) -> dict[str, Any]:
        """Build the OpenAI chat-completions JSON body for a captcha image."""
        encoded = base64.b64encode(screenshot_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{encoded}"
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            # Bound output so the model can't ramble; 16 tokens is more than
            # enough for a 4-5 char answer plus any minor framing tokens the
            # model emits before settling on the bare text.
            "max_tokens": 16,
            "temperature": 0,
        }

    @staticmethod
    def _extract_answer(data: Any) -> str | None:
        """Pull ``choices[0].message.content``, strip + validate it.

        Returns the validated answer (still in whatever case the model
        emitted) or ``None`` when the response is malformed, empty, or
        does not match the answer regex.
        """
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None
        stripped = content.strip()
        if not stripped:
            return None
        if not _ANSWER_RE.match(stripped):
            return None
        return stripped


__all__ = ["DEFAULT_BASE_URL", "OpenAIVLMSolver"]
