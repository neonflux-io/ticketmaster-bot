"""Random-pick strategy.

Shuffles the parsed candidate list and clicks the first row that
survives the ``max_price`` cap (if any). When ``seed`` is provided, the
shuffle is deterministic so the strategy can be exercised by tests
without flakiness.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class RandomPickStrategy(SelectionStrategy):
    """Shuffle quick-pick rows and click the first one under ``max_price``.

    Parameters
    ----------
    seed:
        When set, the per-instance shuffle is deterministic (each call to
        :meth:`pick` produces the same first survivor for the same input
        DOM). Leave ``None`` for true randomness across runs.
    max_price:
        Optional inclusive upper bound. Rows above the cap (and rows
        whose price could not be parsed when a cap is set) are skipped.
    """

    def __init__(
        self,
        seed: int | None = None,
        *,
        max_price: float | None = None,
    ) -> None:
        self.seed = seed
        self.max_price = max_price

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        rng = random.Random(self.seed)
        order = list(range(len(candidates)))
        rng.shuffle(order)

        for i in order:
            candidate = candidates[i]
            if self.max_price is not None:
                if candidate.price is None or candidate.price > self.max_price:
                    continue
            log.info(
                "Random-pick (seed=%s, max_price=%s): %s",
                self.seed,
                self.max_price,
                candidate,
            )
            if not await self.click_candidate(candidate, page):
                log.error("Failed to click random-pick candidate after retries")
                return None
            return candidate

        log.warning(
            "No random-pick candidates survived max_price=%s",
            self.max_price,
        )
        return None
