"""Generic JSON-over-HTTP webhook notifier.

``WebhookNotifier`` POSTs a structured JSON payload describing a
:class:`~src.notifiers.base.NotifyEvent` to an arbitrary HTTP endpoint.
It is the building block downstream of more opinionated notifiers
(Discord, Slack, ...) - those wrap a webhook URL but encode their own
payload shape, while this one ships the canonical "event envelope" any
generic consumer can react to.

Retry policy
------------
Each POST is attempted at most ``max_attempts`` times (default 3). The
notifier retries only when the failure is plausibly transient:

* ``httpx.TimeoutException`` (any subclass: connect/read/write/pool)
* ``httpx.NetworkError`` (connection refused, DNS, etc.)
* Server responses with status in ``[500, 600)``

Anything else (most importantly any ``4xx`` response) is surfaced as a
:class:`~src.notifiers.base.NotifierError` immediately - we never retry on
4xx because those indicate a malformed request and re-sending will not help.
Between attempts the notifier sleeps ``backoff_base * 2**(attempt-1)``
seconds (capped at ``backoff_max``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import Notifier, NotifierError, NotifyEvent

log = logging.getLogger("ticketmaster-bot")


class WebhookNotifier(Notifier):
    """Generic webhook notifier that POSTs JSON to ``url``.

    Parameters
    ----------
    url:
        Fully-qualified HTTPS (or HTTP, for local tests) endpoint.
    headers:
        Optional extra request headers merged on top of
        ``{"Content-Type": "application/json"}``. The Content-Type cannot
        be downgraded - any caller-supplied value is overwritten so the
        server always sees ``application/json``.
    timeout:
        Per-request total timeout in seconds. Passed through to
        :class:`httpx.Timeout` with a smaller connect timeout (capped at
        ``timeout``) so connection issues fail fast rather than burning
        the whole budget.
    max_attempts:
        Hard cap on total attempts including the first send. Default 3.
    backoff_base:
        Base seconds for exponential backoff (default 1.0). Sleep duration
        between attempt ``n`` and ``n+1`` is ``backoff_base * 2**(n-1)``
        seconds, capped at ``backoff_max``.
    backoff_max:
        Upper bound on backoff sleep duration (default 30 s).
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        if not url:
            raise ValueError("WebhookNotifier requires a non-empty URL")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self.url = url
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

        # Force Content-Type last so callers can't override it.
        merged_headers: dict[str, str] = dict(headers or {})
        merged_headers["Content-Type"] = "application/json"
        self.headers: dict[str, str] = merged_headers

        # Most recent ``httpx.Response`` from a successful POST. Useful for
        # tests that want to inspect status / body without re-issuing the
        # request. ``None`` until :meth:`notify` succeeds at least once.
        self.last_response: httpx.Response | None = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def notify(self, event: NotifyEvent) -> None:
        """POST ``event`` as JSON to :attr:`url`.

        Stores the final ``httpx.Response`` on :attr:`last_response` once a
        2xx lands. Raises :class:`NotifierError` after exhausting all
        attempts or on the first non-retried failure (e.g. any 4xx).
        """
        payload = self.build_payload(event)
        timeout = httpx.Timeout(self.timeout, connect=min(self.timeout, 5.0))

        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    response = await client.post(self.url, json=payload, headers=self.headers)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = exc
                    log.warning(
                        "[webhook] %s attempt %d/%d failed: %s",
                        self.url,
                        attempt,
                        self.max_attempts,
                        exc,
                    )
                    if attempt >= self.max_attempts:
                        break
                    await self._sleep_for_attempt(attempt)
                    continue

                status = response.status_code
                if 200 <= status < 300:
                    self.last_response = response
                    return

                if 500 <= status < 600:
                    last_error = NotifierError(f"webhook POST to {self.url} returned {status}")
                    log.warning(
                        "[webhook] %s attempt %d/%d got %d body=%r",
                        self.url,
                        attempt,
                        self.max_attempts,
                        status,
                        response.text[:200],
                    )
                    if attempt >= self.max_attempts:
                        break
                    await self._sleep_for_attempt(attempt)
                    continue

                # 1xx/3xx/4xx - never retried.
                raise NotifierError(
                    f"webhook POST to {self.url} failed with status {status}: "
                    f"{response.text[:200]!r}"
                )

        if isinstance(last_error, NotifierError):
            raise last_error
        if last_error is not None:
            raise NotifierError(
                f"webhook POST to {self.url} failed after "
                f"{self.max_attempts} attempts: {last_error}"
            ) from last_error
        # Defensive: loop should always either return or raise.
        raise NotifierError(  # pragma: no cover
            f"webhook POST to {self.url} failed without a captured error"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def build_payload(self, event: NotifyEvent) -> dict[str, Any]:
        """Return the canonical JSON-serialisable envelope for ``event``."""
        return {
            "event": event.event_type,
            "title": event.title,
            "message": event.message,
            "severity": event.severity,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "metadata": dict(event.metadata),
        }

    async def _sleep_for_attempt(self, attempt: int) -> None:
        """Sleep with exponential backoff after attempt ``attempt`` (1-based)."""
        delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
        if delay > 0:
            await asyncio.sleep(delay)


__all__ = ["WebhookNotifier"]
