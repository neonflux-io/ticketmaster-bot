"""Abstract base class for ticket selection strategies."""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..registry.selectors import locator_multi

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

log = logging.getLogger("ticketmaster-bot")


_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{1,2})?)")
# Word-bounded so "Section" is not parsed as "sec" + "tion" (the F7.4 worker
# flagged the original ``(?:section|sec)\s*`` form misparsing
# ``Section: GENADM`` as ``TION``). The optional colon supports the SG
# ``Section: GENADM`` label format alongside the US ``Section 108`` shape.
_SECTION_RE = re.compile(r"\b(?:section|sec)\b\s*:?\s*([A-Z0-9]+)", re.IGNORECASE)
_ROW_RE = re.compile(r"\b(?:row)\b\s*:?\s*([A-Z0-9]+)", re.IGNORECASE)


@dataclass
class TicketCandidate:
    """A possible ticket option found on the event page."""

    locator: Locator
    price: float | None
    section: str | None
    row: str | None
    description: str
    sections: list[str] = field(default_factory=list)
    price_level_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"TicketCandidate(price={self.price}, section={self.section}, "
            f"row={self.row}, desc={self.description!r})"
        )


class SelectionStrategy(ABC):
    """Abstract strategy: given a page, pick a ticket and click it."""

    @abstractmethod
    async def pick(self, page: Page) -> TicketCandidate | None:
        """Find and click the chosen ticket. Returns the candidate, or None."""

    async def list_candidates(self, page: Page) -> list[TicketCandidate]:
        """Read all ticket rows from the quick-picks list."""
        candidates: list[TicketCandidate] = []
        rows = locator_multi(page, "quick_pick_row")
        try:
            count = await rows.count()
        except Exception:  # noqa: BLE001
            return []
        for i in range(count):
            row = rows.nth(i)
            try:
                text = (await row.inner_text(timeout=2000)).strip()
            except Exception:  # noqa: BLE001
                continue
            price = _extract_price(text)
            sections = _extract_sections(text)
            row_label = _extract_row(text)
            try:
                price_level_id = await row.get_attribute("data-price-level-id", timeout=200)
            except Exception:  # noqa: BLE001
                price_level_id = None
            candidates.append(
                TicketCandidate(
                    locator=row,
                    price=price,
                    section=sections[0] if sections else None,
                    row=row_label,
                    description=text.replace("\n", " | ")[:200],
                    sections=sections,
                    price_level_id=price_level_id,
                )
            )
        return candidates

    async def click_candidate(
        self, candidate: TicketCandidate, page: Page, attempts: int = 2
    ) -> bool:
        """Click a candidate, re-resolving against the page on stale-locator errors."""
        for attempt in range(1, attempts + 1):
            try:
                await candidate.locator.scroll_into_view_if_needed(timeout=4000)
                await candidate.locator.click(timeout=4000)
                return True
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "Candidate click attempt %d failed (%s); refreshing candidates",
                    attempt,
                    exc,
                )
                if attempt >= attempts:
                    break
                # Re-resolve by description match against a freshly-listed set.
                fresh = await self.list_candidates(page)
                replacement = _find_match(fresh, candidate)
                if replacement is None:
                    await asyncio.sleep(0.25)
                    continue
                candidate.locator = replacement.locator
        return False


# --- text helpers ----------------------------------------------------------


def _extract_price(text: str) -> float | None:
    match = _PRICE_RE.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_sections(text: str) -> list[str]:
    return [m.group(1).upper() for m in _SECTION_RE.finditer(text)]


def _extract_section(text: str) -> str | None:
    sections = _extract_sections(text)
    return sections[0] if sections else None


def _extract_row(text: str) -> str | None:
    match = _ROW_RE.search(text)
    if match:
        return match.group(1).upper()
    return None


def _find_match(
    candidates: list[TicketCandidate], target: TicketCandidate
) -> TicketCandidate | None:
    for c in candidates:
        if c.description == target.description:
            return c
    for c in candidates:
        if c.price == target.price and c.section == target.section and c.row == target.row:
            return c
    return None
