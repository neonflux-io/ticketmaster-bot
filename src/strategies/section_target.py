"""Section-target strategy - pick tickets in a specific section/row/price level."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..utils.config_loader import SectionTargetConfig

log = logging.getLogger("ticketmaster-bot")


class SectionTargetStrategy(SelectionStrategy):
    """Pick a ticket matching configured section / row range / price level."""

    def __init__(
        self,
        target: SectionTargetConfig,
        max_price: float | None = None,
    ) -> None:
        self.target = target
        self.max_price = max_price

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        target_section = self.target.section.upper() if self.target.section else None
        row_lo, row_hi = _normalize_row_range(self.target.row_range)
        target_price_level = self.target.price_level_id

        matches: list[TicketCandidate] = []
        for c in candidates:
            if target_section:
                sections = {s.upper() for s in c.sections} or (
                    {c.section.upper()} if c.section else set()
                )
                if target_section not in sections:
                    continue
            if row_lo is not None and row_hi is not None:
                if c.row is None or not (row_lo <= c.row.upper() <= row_hi):
                    continue
            if target_price_level is not None:
                if (c.price_level_id or "") != str(target_price_level):
                    continue
            if self.max_price is not None and c.price is not None and c.price > self.max_price:
                continue
            matches.append(c)

        if not matches:
            log.warning(
                "No candidates matched section=%s row_range=%s price_level_id=%s (max_price=%s)",
                self.target.section,
                self.target.row_range,
                self.target.price_level_id,
                self.max_price,
            )
            return None

        matches.sort(key=lambda c: c.price or float("inf"))
        chosen = matches[0]
        log.info("Section-target pick: %s", chosen)
        if not await self.click_candidate(chosen, page):
            log.error("Failed to click section-target candidate after retries")
            return None
        return chosen


def _normalize_row_range(
    rng: list[str] | None,
) -> tuple[str | None, str | None]:
    if not rng or len(rng) != 2:
        return None, None
    lo, hi = rng[0].upper(), rng[1].upper()
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi
