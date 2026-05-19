"""Cheapest-available strategy."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class CheapestStrategy(SelectionStrategy):
    """Pick the lowest-priced ticket below max_price (if set)."""

    def __init__(self, max_price: float | None = None) -> None:
        self.max_price = max_price

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        priced = [c for c in candidates if c.price is not None]
        if not priced:
            log.warning("No priced candidates; falling back to first candidate")
            chosen = candidates[0]
        else:
            priced.sort(key=lambda c: c.price or float("inf"))
            chosen = priced[0]

        if self.max_price is not None and chosen.price is not None:
            if chosen.price > self.max_price:
                log.warning(
                    "Cheapest ticket ($%.2f) exceeds max_price ($%.2f)",
                    chosen.price,
                    self.max_price,
                )
                return None

        log.info("Cheapest pick: %s", chosen)
        clicked = await self.click_candidate(chosen, page)
        if not clicked:
            log.error("Failed to click cheapest candidate after retries")
            return None
        return chosen
