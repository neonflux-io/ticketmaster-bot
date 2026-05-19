"""Price-range strategy - pick the cheapest ticket within a price band."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class PriceRangeStrategy(SelectionStrategy):
    """Filter quick-pick rows by ``[min_price, max_price]`` (both inclusive)
    and click the cheapest survivor.

    Either bound may be ``None`` to leave that side of the band open.
    Rows whose price could not be parsed are skipped entirely, since the
    point of the strategy is the price constraint.
    """

    def __init__(
        self,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> None:
        if (
            min_price is not None
            and max_price is not None
            and min_price > max_price
        ):
            raise ValueError(
                f"PriceRangeStrategy: min_price ({min_price}) must be <= "
                f"max_price ({max_price})"
            )
        self.min_price = min_price
        self.max_price = max_price

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        in_band: list[TicketCandidate] = []
        for c in candidates:
            if c.price is None:
                continue
            if self.min_price is not None and c.price < self.min_price:
                continue
            if self.max_price is not None and c.price > self.max_price:
                continue
            in_band.append(c)

        if not in_band:
            log.warning(
                "No candidates priced in [%s, %s]",
                self.min_price,
                self.max_price,
            )
            return None

        in_band.sort(key=lambda c: c.price or float("inf"))
        chosen = in_band[0]
        log.info(
            "Price-range pick (band=[%s, %s]): %s",
            self.min_price,
            self.max_price,
            chosen,
        )
        if not await self.click_candidate(chosen, page):
            log.error("Failed to click price-range candidate after retries")
            return None
        return chosen
