"""Pre-flight cookie warmer.

``warm(context, target_seconds, pages, …)`` opens 2-3 unrelated
Ticketmaster-style pages in the caller-supplied :class:`BrowserContext`,
dwelling on each for a randomised fraction of ``target_seconds`` so the
total wait time matches the requested target. Pages are loaded with
``page.goto`` and closed before the function returns, leaving the
context's page count unchanged.

Purpose: a fresh persistent profile that immediately hits the queue/cart
flow without any history looks suspicious. Spending half a minute on
the home page, the events index and the sports section first plants
cookies, runs the front-end fingerprinting JS, and produces realistic
referrer / cache state before the bot starts the actual flow.

Public surface:

* :func:`warm`
* :data:`DEFAULT_PAGES`
* :data:`DEFAULT_TARGET_SECONDS`
* :data:`MIN_VISITS`, :data:`MAX_VISITS`
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

log = logging.getLogger("ticketmaster-bot")

# Feature-spec defaults: visit ticketmaster.com home + /events + /sports.
DEFAULT_PAGES: tuple[str, ...] = (
    "https://www.ticketmaster.com/",
    "https://www.ticketmaster.com/events",
    "https://www.ticketmaster.com/sports",
)

# Feature-spec defaults.
DEFAULT_TARGET_SECONDS: float = 30.0
MIN_VISITS: int = 2
MAX_VISITS: int = 3

# Per-step page-load timeout (ms). The warmer is best-effort: if a page
# is slow or 404s, we record the failure and move on instead of aborting
# the rest of the warm-up.
_GOTO_TIMEOUT_MS: int = 15_000


def _choose_visits(
    pages: Sequence[str],
    *,
    rng: random.Random,
) -> list[str]:
    """Pick 2-3 distinct URLs (or fewer when ``pages`` is shorter)."""
    unique_pages = list(dict.fromkeys(pages))
    if not unique_pages:
        raise ValueError("warmer 'pages' must be a non-empty sequence of URLs")

    target_count = rng.randint(MIN_VISITS, MAX_VISITS)
    target_count = min(target_count, len(unique_pages))
    if target_count < 1:
        target_count = 1
    return rng.sample(unique_pages, target_count)


def _allocate_dwell_seconds(
    total_seconds: float,
    n_visits: int,
    *,
    rng: random.Random,
) -> list[float]:
    """Distribute ``total_seconds`` across ``n_visits`` slots.

    Each slot gets a base share of ``total_seconds / n_visits`` plus a
    random jitter of ±25% of that share, then the slots are rescaled so
    they sum exactly to ``total_seconds``. This produces a noticeable
    per-page variance while still guaranteeing the wall-clock total
    matches what the caller asked for.
    """
    if n_visits <= 0:
        return []
    base = total_seconds / n_visits
    if base <= 0:
        return [0.0] * n_visits
    raw = [base * (1.0 + rng.uniform(-0.25, 0.25)) for _ in range(n_visits)]
    factor = total_seconds / sum(raw)
    return [r * factor for r in raw]


async def _visit(
    context: BrowserContext,
    url: str,
    dwell_seconds: float,
) -> bool:
    """Open ``url`` in a new page, dwell for ``dwell_seconds``, then close.

    Returns ``True`` when the page loaded (any 2xx/3xx) and the dwell
    completed; ``False`` when ``page.goto`` failed. Either way the
    intermediate page is closed so the context's page count is restored.
    """
    page = await context.new_page()
    try:
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=_GOTO_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Warmer goto failed for %s: %s", url, exc)
            return False
        if dwell_seconds > 0:
            await asyncio.sleep(dwell_seconds)
        return True
    finally:
        try:
            await page.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("Warmer page.close failed for %s: %s", url, exc)


async def warm(
    context: BrowserContext,
    *,
    target_seconds: float = DEFAULT_TARGET_SECONDS,
    pages: Sequence[str] = DEFAULT_PAGES,
    seed: int | None = None,
) -> list[str]:
    """Pre-warm ``context`` by visiting 2-3 unrelated pages.

    Parameters
    ----------
    context:
        Live :class:`BrowserContext` from a persistent profile. The
        warmer never opens a new context — it must share cookie / cache
        state with the caller's flow for the warm-up to count.
    target_seconds:
        Total wall-clock time to spend warming up (excluding
        ``goto`` / ``close`` overhead). Must be ≥ 0.
    pages:
        Ordered list of candidate URLs to choose from. Defaults to
        :data:`DEFAULT_PAGES` (Ticketmaster home + /events + /sports).
        Must be non-empty.
    seed:
        Optional RNG seed. Two calls with the same seed against the same
        ``pages`` produce the same URL choice and ordering, which is the
        hook the unit test for determinism relies on.

    Returns
    -------
    list[str]
        The URLs that successfully loaded, in visit order. Returned for
        the caller's bookkeeping; a successful warm-up usually returns
        2-3 entries. URLs that failed to load are omitted.
    """
    if target_seconds < 0:
        raise ValueError(f"warmer target_seconds must be >= 0, got {target_seconds}")
    if not pages:
        raise ValueError("warmer 'pages' must contain at least one URL")

    rng = random.Random(seed) if seed is not None else random.Random()

    chosen = _choose_visits(pages, rng=rng)
    dwell_per_page = _allocate_dwell_seconds(target_seconds, len(chosen), rng=rng)

    visited: list[str] = []
    log.debug(
        "Warmer visiting %d pages over %.2fs: %s",
        len(chosen),
        target_seconds,
        chosen,
    )
    for url, dwell in zip(chosen, dwell_per_page, strict=False):
        ok = await _visit(context, url, dwell)
        if ok:
            visited.append(url)
    return visited


__all__ = [
    "DEFAULT_PAGES",
    "DEFAULT_TARGET_SECONDS",
    "MAX_VISITS",
    "MIN_VISITS",
    "warm",
]
