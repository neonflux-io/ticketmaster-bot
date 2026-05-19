"""Accessible-seat strategy.

Filters quick-pick rows down to those carrying an accessible marker
(either the accessible-seat data attribute on the row or a descendant,
or an ``aria-label`` containing the word "accessible"
case-insensitively). Among the survivors, the cheapest priced row is
clicked. The selector that powers the filter lives at
``accessible_seat_marker`` in ``config/selectors/ticketmaster.yaml`` so
nothing about the DOM contract is hard-coded in Python.

The ``accessible_seats`` flag mirrors ``tickets.accessible_seats`` in
:mod:`src.utils.config_loader`: when ``False`` the accessibility filter
is bypassed entirely and the strategy degrades into a plain cheapest
pick, so the user's existing config keeps working unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..registry.selectors import selector_for
from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class AccessibleStrategy(SelectionStrategy):
    """Pick the cheapest row that advertises accessible seating.

    Parameters
    ----------
    max_price:
        Optional inclusive upper bound; rows above it are skipped.
    accessible_seats:
        Mirrors ``tickets.accessible_seats``. ``True`` (the default)
        requires every candidate to carry an accessibility marker.
        ``False`` keeps the strategy registered but bypasses the
        filter, so callers can drive it generically from
        :func:`src.strategies.factory.build_strategy` without
        special-casing.
    """

    def __init__(
        self,
        *,
        max_price: float | None = None,
        accessible_seats: bool = True,
    ) -> None:
        self.max_price = max_price
        self.accessible_seats = accessible_seats

    async def pick(self, page: Page) -> TicketCandidate | None:
        candidates = await self.list_candidates(page)
        if not candidates:
            log.warning("No ticket candidates found on page")
            return None

        if self.accessible_seats:
            survivors = await _filter_accessible(page, candidates)
            if not survivors:
                log.warning(
                    "No accessible-seat rows found (looked for %r)",
                    selector_for("accessible_seat_marker"),
                )
                return None
        else:
            survivors = list(candidates)

        if self.max_price is not None:
            survivors = [
                c for c in survivors
                if c.price is not None and c.price <= self.max_price
            ]
            if not survivors:
                log.warning(
                    "All accessible candidates exceeded max_price=%s",
                    self.max_price,
                )
                return None

        survivors.sort(key=lambda c: c.price if c.price is not None else float("inf"))
        chosen = survivors[0]
        log.info(
            "Accessible pick (accessible_seats=%s, max_price=%s): %s",
            self.accessible_seats,
            self.max_price,
            chosen,
        )
        if not await self.click_candidate(chosen, page):
            log.error("Failed to click accessible candidate after retries")
            return None
        return chosen


async def _filter_accessible(
    page: Page, candidates: list[TicketCandidate]
) -> list[TicketCandidate]:
    """Return the subset of ``candidates`` whose row advertises accessibility.

    The accessibility check is performed in-page by evaluating each row
    against the registry-defined ``accessible_seat_marker`` selector.
    A row qualifies when either the row itself or any of its descendants
    matches the selector.
    """
    marker_selector = selector_for("accessible_seat_marker")
    survivors: list[TicketCandidate] = []
    for candidate in candidates:
        try:
            is_accessible = await candidate.locator.evaluate(
                """
                (node, selector) => {
                    if (!node) return false;
                    try {
                        if (node.matches && node.matches(selector)) return true;
                    } catch (e) {
                        // Invalid selector for matches(); fall through.
                    }
                    try {
                        return node.querySelector(selector) !== null;
                    } catch (e) {
                        return false;
                    }
                }
                """,
                marker_selector,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "Accessibility probe failed for candidate %s (%s); skipping",
                candidate.description,
                exc,
            )
            continue
        if is_accessible:
            survivors.append(candidate)
    return survivors
