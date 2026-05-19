"""Best-available strategy - uses Ticketmaster's own 'best available' button."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate, _extract_price

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class BestAvailableStrategy(SelectionStrategy):
    """Click the 'Best Available' / first quick-pick option, respecting max_price."""

    def __init__(self, max_price: float | None = None) -> None:
        self.max_price = max_price

    async def pick(self, page: Page) -> TicketCandidate | None:
        # First try a dedicated "Best Available" button.
        try:
            best_btn = page.locator(
                "button:has-text('Best Available'), [data-bdd='best-available']"
            ).first
            if await best_btn.is_visible(timeout=2000):
                log.info("Clicking 'Best Available' button")
                await best_btn.click()
                # Try to read the resulting price from the order summary.
                resolved_price = await _read_selected_price(page)
                if (
                    self.max_price is not None
                    and resolved_price is not None
                    and resolved_price > self.max_price
                ):
                    log.warning(
                        "Best-available rolled $%.2f > max_price $%.2f - aborting",
                        resolved_price,
                        self.max_price,
                    )
                    return None
                if self.max_price is not None and resolved_price is None:
                    log.warning(
                        "Best-available picked but price could not be verified "
                        "against max_price; aborting for safety"
                    )
                    return None
                return TicketCandidate(
                    locator=best_btn,
                    price=resolved_price,
                    section=None,
                    row=None,
                    description=f"Best Available (TM button) @ ${resolved_price}",
                )
        except Exception:  # noqa: BLE001
            pass

        # Otherwise pick the first ticket-list entry (TM orders by best available).
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        chosen = candidates[0]
        if (
            self.max_price is not None
            and chosen.price is not None
            and chosen.price > self.max_price
        ):
            log.warning(
                "Best-available price ($%.2f) exceeds max_price ($%.2f)",
                chosen.price,
                self.max_price,
            )
            return None

        log.info("Best-available pick: %s", chosen)
        if not await self.click_candidate(chosen, page):
            log.error("Failed to click best-available candidate after retries")
            return None
        return chosen


async def _read_selected_price(page: Page) -> float | None:
    """Best-effort read the post-selection price from the order summary widgets."""
    selectors = (
        "[data-bdd='order-summary']",
        "[data-bdd='total-price']",
        "[data-bdd='subtotal']",
        "[class*='total']",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=750):
                text = (await loc.inner_text(timeout=750)) or ""
                price = _extract_price(text)
                if price is not None:
                    return price
        except Exception:  # noqa: BLE001
            continue
    return None
