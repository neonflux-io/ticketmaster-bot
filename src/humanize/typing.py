"""Human-cadence typing helpers.

The public entry point is :func:`human_type`. It types ``text`` into a
Playwright :class:`Locator` one character at a time via
``page.keyboard.type(char, delay=...)`` with a normally-distributed
per-keystroke delay drawn from ``Normal(mean_ms, std_ms)`` and floored
at ``min_ms``. Replaces ``locator.fill(text)`` for credential fields
when ``timing.humanize.typing.enabled`` so login flows produce a
plausible keystroke timing profile instead of a single instant fill.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Locator

# Defaults match the F4.2 spec: mean 70 ms, sigma 25 ms, floor 20 ms.
DEFAULT_MEAN_MS = 70.0
DEFAULT_STD_MS = 25.0
DEFAULT_MIN_MS = 20.0


def _next_delay_ms(*, mean_ms: float, std_ms: float, min_ms: float, rng: random.Random) -> int:
    """Sample a per-keystroke delay in integer milliseconds.

    The raw sample is drawn from ``Normal(mean_ms, std_ms)`` and floored
    at ``min_ms`` so wide variances still produce delays Playwright will
    actually honour. The result is rounded to the nearest integer
    millisecond because that's the unit ``keyboard.type(delay=...)``
    expects.
    """
    if std_ms == 0:
        raw = mean_ms
    else:
        raw = rng.gauss(mean_ms, std_ms)
    if raw < min_ms:
        raw = min_ms
    return int(round(raw))


async def human_type(
    locator: Locator,
    text: str,
    *,
    mean_ms: float = DEFAULT_MEAN_MS,
    std_ms: float = DEFAULT_STD_MS,
    min_ms: float = DEFAULT_MIN_MS,
    seed: int | None = None,
) -> None:
    """Type ``text`` into ``locator`` one character at a time.

    Parameters
    ----------
    locator:
        Playwright :class:`Locator` for the input element that should
        receive the text. The locator is focused before typing begins
        so the keystrokes target it specifically (matches the behaviour
        of ``locator.type`` in Playwright).
    text:
        Characters to type. Empty input is a no-op.
    mean_ms:
        Mean per-keystroke delay in milliseconds for the normal
        distribution. Must be > 0.
    std_ms:
        Standard deviation in milliseconds. Must be ≥ 0.
    min_ms:
        Floor applied after sampling. Must be ≥ 0. Guarantees that the
        delta between two consecutive keydown events is at least this
        many milliseconds, which makes timing-based humanisation
        checks deterministic to assert.
    seed:
        Optional RNG seed for deterministic tests.
    """
    if mean_ms <= 0:
        raise ValueError(f"human_type mean_ms must be > 0, got {mean_ms}")
    if std_ms < 0:
        raise ValueError(f"human_type std_ms must be >= 0, got {std_ms}")
    if min_ms < 0:
        raise ValueError(f"human_type min_ms must be >= 0, got {min_ms}")

    if not text:
        return

    rng = random.Random(seed) if seed is not None else random.Random()

    # Focus the target so keyboard.type lands in the right element.
    await locator.focus()
    page = locator.page
    for char in text:
        delay = _next_delay_ms(mean_ms=mean_ms, std_ms=std_ms, min_ms=min_ms, rng=rng)
        # ``page.keyboard.type`` accepts a ``delay`` (in ms) applied
        # *between* keystrokes; calling it once per character with the
        # sampled delay both inserts the char and waits the configured
        # interval before returning, producing the per-key timing
        # distribution we want.
        await page.keyboard.type(char, delay=delay)


__all__ = ["DEFAULT_MEAN_MS", "DEFAULT_MIN_MS", "DEFAULT_STD_MS", "human_type"]
