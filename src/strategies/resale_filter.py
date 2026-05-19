"""Resale-filter wrapper strategy.

``ResaleFilterStrategy`` is a decorator: it wraps any other
:class:`~src.strategies.base.SelectionStrategy` and pre-filters the
quick-pick rows by the presence (``include_resale=True``) or absence
(``exclude_resale=True``) of the ``resale_tag`` marker (see
``config/selectors/ticketmaster.yaml``) before letting the inner
strategy run against the surviving subset.

The pre-filter is implemented by temporarily renaming the row-marker
attribute (the logical-name ``quick_pick_row`` registry entry) on every
row that should be hidden, so the inner strategy's selector simply
cannot find them. The original attribute value is stashed on a
sibling attribute so the wrapper can put it back. Restoration runs in
a ``finally`` block so the DOM is never left in a weird state, even if
the inner strategy raises.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..registry.selectors import selector_for
from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class ResaleFilterStrategy(SelectionStrategy):
    """Wrap ``inner`` so it only sees rows on the right side of the resale gate.

    Exactly one of ``include_resale`` / ``exclude_resale`` must be set;
    setting both (or neither) is treated as a configuration mistake and
    raises :class:`ValueError`.

    Parameters
    ----------
    inner:
        The strategy that will actually pick a candidate.
    include_resale:
        Keep only rows that carry a resale marker; non-resale rows are
        hidden from the inner strategy.
    exclude_resale:
        Drop every row that carries a resale marker; only primary-market
        rows reach the inner strategy.
    """

    def __init__(
        self,
        inner: SelectionStrategy,
        *,
        include_resale: bool = False,
        exclude_resale: bool = False,
    ) -> None:
        if include_resale and exclude_resale:
            raise ValueError(
                "ResaleFilterStrategy: set exactly one of include_resale "
                "or exclude_resale, not both"
            )
        if not include_resale and not exclude_resale:
            raise ValueError(
                "ResaleFilterStrategy: must set one of include_resale or exclude_resale"
            )
        self.inner = inner
        self.include_resale = include_resale
        self.exclude_resale = exclude_resale

    async def pick(self, page: Page) -> TicketCandidate | None:
        marker_selector = selector_for("resale_tag")
        row_selector = selector_for("quick_pick_row")
        try:
            hidden_count = await _hide_rows(
                page,
                row_selector=row_selector,
                marker_selector=marker_selector,
                hide_resale=self.exclude_resale,
                hide_non_resale=self.include_resale,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ResaleFilterStrategy: failed to apply resale filter (%s); "
                "running inner strategy unfiltered",
                exc,
            )
            return await self.inner.pick(page)

        log.info(
            "Resale filter: %s (hid %d row(s))",
            "include_resale" if self.include_resale else "exclude_resale",
            hidden_count,
        )

        try:
            return await self.inner.pick(page)
        finally:
            try:
                await _restore_rows(page)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "ResaleFilterStrategy: failed to restore hidden rows (%s)",
                    exc,
                )


async def _hide_rows(
    page: Page,
    *,
    row_selector: str,
    marker_selector: str,
    hide_resale: bool,
    hide_non_resale: bool,
) -> int:
    """Hide rows from the inner strategy by stripping their ``data-bdd``.

    Returns the number of rows that were hidden. The original value of
    every row's ``data-bdd`` attribute is stashed on
    ``data-bdd-original`` so :func:`_restore_rows` can put it back.
    """
    return int(
        await page.evaluate(
            """
            ({rowSelector, markerSelector, hideResale, hideNonResale}) => {
                const rows = Array.from(document.querySelectorAll(rowSelector));
                let hidden = 0;
                for (const row of rows) {
                    let isResale = false;
                    try {
                        if (row.matches && row.matches(markerSelector)) {
                            isResale = true;
                        }
                    } catch (e) {
                        // ignore matches() failures on exotic selectors
                    }
                    if (!isResale) {
                        try {
                            isResale = row.querySelector(markerSelector) !== null;
                        } catch (e) {
                            isResale = false;
                        }
                    }
                    const shouldHide =
                        (isResale && hideResale) || (!isResale && hideNonResale);
                    if (!shouldHide) continue;
                    const original = row.getAttribute('data-bdd');
                    if (original !== null) {
                        row.setAttribute('data-bdd-original', original);
                        row.removeAttribute('data-bdd');
                    }
                    row.setAttribute('data-bdd-resale-hidden', '1');
                    hidden += 1;
                }
                return hidden;
            }
            """,
            {
                "rowSelector": row_selector,
                "markerSelector": marker_selector,
                "hideResale": hide_resale,
                "hideNonResale": hide_non_resale,
            },
        )
    )


async def _restore_rows(page: Page) -> None:
    """Undo whatever :func:`_hide_rows` did."""
    await page.evaluate(
        """
        () => {
            const hidden = Array.from(
                document.querySelectorAll('[data-bdd-resale-hidden]')
            );
            for (const row of hidden) {
                const original = row.getAttribute('data-bdd-original');
                if (original !== null) {
                    row.setAttribute('data-bdd', original);
                    row.removeAttribute('data-bdd-original');
                }
                row.removeAttribute('data-bdd-resale-hidden');
            }
        }
        """
    )
