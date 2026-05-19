"""Composite strategy - try each child in order, return the first match.

Delegation lets callers chain multiple selection strategies under a
single :class:`SelectionStrategy` interface. The first child whose
:meth:`SelectionStrategy.pick` returns a non-``None`` candidate wins,
its returned candidate is what the composite returns, and no later
child is consulted (or asked to click). When every child returns
``None`` the composite returns ``None`` as well.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class CompositeStrategy(SelectionStrategy):
    """Run an ordered list of child strategies and return the first hit.

    Parameters
    ----------
    children:
        Strategies to try, in order. Must be non-empty - a composite
        with no children would always return ``None``, which is almost
        certainly a configuration mistake worth surfacing.
    """

    def __init__(self, children: Sequence[SelectionStrategy]) -> None:
        if not children:
            raise ValueError("CompositeStrategy requires a non-empty children list")
        # Defensive copy so external mutation of the caller's list does
        # not change the composite's behavior mid-run.
        self.children: list[SelectionStrategy] = list(children)

    async def pick(self, page: Page) -> TicketCandidate | None:
        for index, child in enumerate(self.children):
            log.debug(
                "Composite: trying child %d/%d (%s)",
                index + 1,
                len(self.children),
                type(child).__name__,
            )
            chosen = await child.pick(page)
            if chosen is not None:
                log.info(
                    "Composite pick from child %d (%s): %s",
                    index + 1,
                    type(child).__name__,
                    chosen,
                )
                return chosen

        log.warning(
            "Composite: no child returned a candidate (%d tried)",
            len(self.children),
        )
        return None
