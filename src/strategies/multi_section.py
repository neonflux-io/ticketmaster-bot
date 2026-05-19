"""Multi-section preference strategy.

Walks an ordered list of sections; the first section that has at least
one matching quick-pick row wins. When several rows match the winning
section, the cheapest is selected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class MultiSectionStrategy(SelectionStrategy):
    """Iterate ``sections`` in order; return the first section's cheapest row.

    A row matches a section when any of the section tokens parsed out of
    its text (uppercased) equals the preference (also uppercased). When
    ``max_price`` is set, rows exceeding it are excluded before the
    cheapest-per-section decision is made; if no row in the current
    preference survives, the next preference is tried.
    """

    def __init__(
        self,
        sections: list[str],
        max_price: float | None = None,
    ) -> None:
        if not sections:
            raise ValueError("MultiSectionStrategy requires a non-empty sections list")
        # Preserve the caller-supplied order; uppercase for comparison.
        self.sections: list[str] = [s.upper() for s in sections]
        self.max_price = max_price

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        for preference in self.sections:
            matches: list[TicketCandidate] = []
            for c in candidates:
                if not _row_matches_section(c, preference):
                    continue
                if self.max_price is not None and c.price is not None and c.price > self.max_price:
                    continue
                matches.append(c)

            if not matches:
                log.debug(
                    "No matches for preference %r; trying next",
                    preference,
                )
                continue

            matches.sort(key=lambda c: c.price or float("inf"))
            chosen = matches[0]
            log.info(
                "Multi-section pick (section=%s, preferences=%s): %s",
                preference,
                self.sections,
                chosen,
            )
            if not await self.click_candidate(chosen, page):
                log.error("Failed to click multi-section candidate after retries")
                return None
            return chosen

        log.warning(
            "No candidates matched any section in preferences=%s (max_price=%s)",
            self.sections,
            self.max_price,
        )
        return None


def _row_matches_section(candidate: TicketCandidate, preference: str) -> bool:
    """Return ``True`` if ``candidate`` advertises ``preference`` as a section.

    Both the candidate's parsed section tokens and the preference are
    normalised to uppercase before comparison so callers may pass either
    ``"100"`` or ``"Floor"`` regardless of how the DOM text was cased.
    """
    parsed = {s.upper() for s in candidate.sections}
    if candidate.section:
        parsed.add(candidate.section.upper())
    return preference in parsed
