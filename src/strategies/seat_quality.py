"""Seat-quality strategy.

Scores every quick-pick row by:

1. The ``data-quality-score`` attribute on the row (cast to ``float``),
   if present. This is a direct signal from the venue.
2. Each token in :data:`_QUALITY_TOKENS` that appears in the row's
   text (case-insensitive whole-word match) contributes ``+1.0``.

The highest-scoring surviving row is clicked. Ties are broken by the
cheaper row winning so on-budget seats edge out their more expensive
twins. ``max_price`` (inclusive) excludes rows above the cap before
scoring runs.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")

# Text tokens that signal a high-quality seat. Matched as whole words,
# case-insensitively, against the row's visible text.
_QUALITY_TOKENS: tuple[str, ...] = ("floor", "center", "aisle", "front")


class SeatQualityStrategy(SelectionStrategy):
    """Pick the highest-quality quick-pick row.

    Parameters
    ----------
    max_price:
        Optional inclusive upper bound. Rows above the cap are excluded
        from the candidate pool before scoring.
    tokens:
        Override the default quality-token list. Lower-cased on
        construction so callers may pass mixed case freely.
    """

    def __init__(
        self,
        *,
        max_price: float | None = None,
        tokens: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.max_price = max_price
        self.tokens: tuple[str, ...] = tuple(
            t.lower() for t in (tokens if tokens is not None else _QUALITY_TOKENS)
        )

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        survivors: list[TicketCandidate] = []
        if self.max_price is not None:
            for c in candidates:
                if c.price is None or c.price > self.max_price:
                    continue
                survivors.append(c)
            if not survivors:
                log.warning(
                    "All seat-quality candidates exceeded max_price=%s",
                    self.max_price,
                )
                return None
        else:
            survivors = list(candidates)

        scored: list[tuple[float, float, int, TicketCandidate]] = []
        for index, candidate in enumerate(survivors):
            score = await _score_candidate(candidate, self.tokens)
            # Negate price so the sort (ascending on the tuple) prefers
            # cheaper rows when scores tie. ``index`` is the final
            # tiebreaker so the call is deterministic even when both
            # score and price tie.
            tie_price = candidate.price if candidate.price is not None else float("inf")
            scored.append((-score, tie_price, index, candidate))
        scored.sort()
        _, _, _, chosen = scored[0]

        log.info(
            "Seat-quality pick (max_price=%s, tokens=%s): %s",
            self.max_price,
            list(self.tokens),
            chosen,
        )
        if not await self.click_candidate(chosen, page):
            log.error("Failed to click seat-quality candidate after retries")
            return None
        return chosen


async def _score_candidate(candidate: TicketCandidate, tokens: tuple[str, ...]) -> float:
    """Compute ``candidate``'s quality score."""
    score = 0.0
    try:
        raw_attr = await candidate.locator.get_attribute("data-quality-score", timeout=200)
    except Exception:  # noqa: BLE001
        raw_attr = None
    if raw_attr is not None:
        try:
            score += float(raw_attr)
        except ValueError:
            log.debug(
                "Non-numeric data-quality-score=%r on %s; ignoring",
                raw_attr,
                candidate.description,
            )

    text = candidate.description or ""
    for token in tokens:
        if _token_in_text(text, token):
            score += 1.0
    return score


def _token_in_text(text: str, token: str) -> bool:
    """Whole-word, case-insensitive token presence check."""
    if not token:
        return False
    return re.search(rf"\b{re.escape(token)}\b", text, flags=re.IGNORECASE) is not None
