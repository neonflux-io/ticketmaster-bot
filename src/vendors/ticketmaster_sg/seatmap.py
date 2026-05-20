"""SG-specific interactive seat-map strategy.

The shared :class:`src.strategies.interactive_seatmap.InteractiveSeatmapStrategy`
matches ``<rect data-section data-row data-seat>`` elements inside an
SVG seat-map (the shape Ticketmaster's US React app uses). The SG site
does NOT use SVG: F8.1 recon
(``docs/recon/ticketmaster_sg/seatmap.md``) shows the per-section
seat-map is a Yii-rendered ``<table class="seat">`` whose ``<td>``
cells carry:

* ``class="empty"`` / ``"sold"`` / ``"noseat"`` / ``"checked"`` /
  ``"tagseat"`` — the visible state.
* ``data-coordinate="<row_idx>_<col_idx>"`` — the internal grid
  coordinate (1-based, used by the inline AJAX submit).
* ``data-seatrow="<n>"`` — the visible row label the operator
  configures against (``tickets.interactive_seatmap.row``).
* ``data-seatno="<n>"`` — the visible seat number
  (``tickets.interactive_seatmap.seat``).

The table is mounted inside a Fancybox iframe whose ``src`` matches
``/ticket/select-seat/<gameCode>/<dateId>/<areaNo>/<count>``. The
parent page URL stays on ``/ticket/area/<...>`` while the iframe loads,
so the strategy looks for the iframe first and only falls back to the
top-level page when no iframe is present (the F8.2 fixture case loads
the seat-map HTML directly into a top-level page after neutering the
inline ``window.document == parent.document`` guard).

Every selector — including the parametric
``seatmap_seat_by_label_template`` — lives in
``config/selectors/ticketmaster_sg.yaml`` and is resolved via the
SG-scoped helper in :mod:`src.vendors.ticketmaster_sg.selectors`, so
no inline CSS strings leak into ``src/``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...strategies.base import SelectionStrategy, TicketCandidate
from .selectors import selector_for, selector_for_template, try_selector_for

if TYPE_CHECKING:
    from playwright.async_api import FrameLocator, Locator, Page

log = logging.getLogger("ticketmaster-bot")


class SGInteractiveSeatmapStrategy(SelectionStrategy):
    """Click the SG seat-map ``<td>`` matching ``(row, seat)`` in ``section``.

    Parameters
    ----------
    section:
        Visible section label (e.g. ``"225"``). Carried verbatim on the
        returned :class:`TicketCandidate` for log greppability — the
        SG seat-map iframe is already scoped to a single section by URL
        (one ``areaNo`` per iframe), so the selector itself does NOT
        filter by section. The seat-map only renders cells inside the
        section the operator drilled into upstream.
    row, seat:
        Non-empty visible labels matched verbatim against the
        ``data-seatrow`` / ``data-seatno`` attributes via the
        ``seatmap_seat_by_label_template`` selector. Strings, not ints —
        the SG DOM emits them as strings and CSS attribute matching is
        string-equality.

    Raises
    ------
    ValueError
        When any of ``section``, ``row``, ``seat`` is empty / whitespace.
    """

    def __init__(self, *, section: str, row: str, seat: str) -> None:
        if not section or not section.strip():
            raise ValueError("SGInteractiveSeatmapStrategy.section must be non-empty")
        if not row or not row.strip():
            raise ValueError("SGInteractiveSeatmapStrategy.row must be non-empty")
        if not seat or not seat.strip():
            raise ValueError("SGInteractiveSeatmapStrategy.seat must be non-empty")
        self.section = section
        self.row = row
        self.seat = seat

    async def pick(self, page: Page) -> TicketCandidate | None:
        scope = await _resolve_seatmap_scope(page)
        if scope is None:
            log.warning(
                "SG seat-map not present on page (no iframe matching "
                "seatmap_iframe and no top-level table.seat); cannot pick "
                "section=%s row=%s seat=%s",
                self.section,
                self.row,
                self.seat,
            )
            return None

        cell_selector = selector_for_template(
            "seatmap_seat_by_label_template",
            row=self.row,
            seat=self.seat,
        )
        cell: Locator = scope.locator(cell_selector).first

        if not await _cell_present(cell):
            log.warning(
                "SG seat-map: no available cell at section=%s row=%s seat=%s "
                "(selector=%r) — either the seat is sold/aisle/already taken "
                "or the row/seat label doesn't exist in this section",
                self.section,
                self.row,
                self.seat,
                cell_selector,
            )
            return None

        try:
            await cell.scroll_into_view_if_needed(timeout=4000)
            await cell.click(timeout=4000)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SG seat-map: failed to click cell at section=%s row=%s seat=%s: %s",
                self.section,
                self.row,
                self.seat,
                exc,
            )
            return None

        description = f"SG seat-map pick: section={self.section} row={self.row} seat={self.seat}"
        log.info("SG seat-map clicked: %s", description)
        return TicketCandidate(
            locator=cell,
            price=None,
            section=self.section,
            row=self.row,
            description=description,
            sections=[self.section],
        )


# --- helpers --------------------------------------------------------------


async def _resolve_seatmap_scope(page: Page) -> Page | FrameLocator | None:
    """Return the scope the seat-grid lives in: the iframe if present, else ``page``.

    In the live SG flow the seat-map is always inside the Fancybox
    iframe at ``/ticket/select-seat/...``. The F8.2 test fixture loads
    the iframe's HTML body directly into a top-level page (after
    neutering the inline ``parent.document == window.document`` guard),
    so this helper supports both shapes.
    """
    iframe_sel = try_selector_for("seatmap_iframe")
    if iframe_sel is not None:
        iframe_locator = page.locator(iframe_sel).first
        try:
            iframe_count = await iframe_locator.count()
        except Exception as exc:  # noqa: BLE001
            log.debug("SG seat-map iframe count probe failed: %s", exc)
            iframe_count = 0
        if iframe_count > 0:
            frame = page.frame_locator(iframe_sel).first
            # Confirm the iframe actually rendered the seat grid before
            # we hand back a FrameLocator the caller would otherwise
            # think is empty.
            try:
                grid_sel = selector_for("seatmap_grid")
                if await frame.locator(grid_sel).count() > 0:
                    return frame
            except Exception as exc:  # noqa: BLE001
                log.debug("SG seat-map iframe grid probe failed: %s", exc)

    # Top-level fallback: the fixture / standalone-load case.
    try:
        grid_sel = selector_for("seatmap_grid")
        if await page.locator(grid_sel).count() > 0:
            return page
    except Exception as exc:  # noqa: BLE001
        log.debug("SG seat-map top-level grid probe failed: %s", exc)

    return None


async def _cell_present(cell: Locator) -> bool:
    try:
        return await cell.count() > 0
    except Exception as exc:  # noqa: BLE001
        log.debug("SG seat-map cell probe failed: %s", exc)
        return False


__all__ = ["SGInteractiveSeatmapStrategy"]
