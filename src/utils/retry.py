"""Retry helpers with exponential backoff."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    factor: float = 2.0,
    jitter: float = 0.25,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Retry an async function with exponential backoff + jitter."""
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - we re-raise after attempts exhaust
            last_exc = exc
            if on_retry:
                on_retry(attempt, exc)
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (factor ** (attempt - 1)))
            delay = delay * (1 + random.uniform(-jitter, jitter))
            await asyncio.sleep(max(0.0, delay))
    raise RetryError(f"Failed after {attempts} attempts") from last_exc


async def random_human_delay(low: float, high: float) -> None:
    """Sleep a random amount between low/high seconds to mimic human pacing."""
    if high <= 0:
        return
    await asyncio.sleep(random.uniform(max(0.0, low), max(low, high)))


async def maybe_human_delay(enabled: bool, low: float, high: float) -> None:
    """Conditional ``random_human_delay`` - no-op when ``enabled`` is False."""
    if not enabled:
        return
    await random_human_delay(low, high)
