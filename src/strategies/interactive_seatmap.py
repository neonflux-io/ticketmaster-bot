"""Interactive seat-map strategy.

Given a fixed ``(section, row, seat)`` tuple, locate and click the
matching ``<rect data-section data-row data-seat>`` element inside the
seat-map SVG. Real Ticketmaster pages render the seat map either inline
on the event page or inside an iframe; this strategy tries the top
frame first and, on miss, falls back to each iframe matched by the
``seatmap_frame`` selector via Playwright's :class:`FrameLocator`.

Selectors live in ``config/selectors/ticketmaster.yaml`` (``seatmap_frame``,
``seatmap_svg``); no DOM string is inlined here.

When no matching rect can be located in any frame, :meth:`pick` returns
``None``. When a click is delivered, the returned
:class:`~src.strategies.base.TicketCandidate` carries the requested
section / row, the matched locator, and a description that includes the
seat number so log lines stay greppable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..registry.selectors import get as get_selectors
from .base import SelectionStrategy, TicketCandidate

if TYPE_CHECKING:
    from playwright.async_api import FrameLocator, Locator, Page

log = logging.getLogger("ticketmaster-bot")


class InteractiveSeatmapStrategy(SelectionStrategy):
    """Click the single ``<rect>`` whose data-attrs match ``(section,row,seat)``.

    Parameters
    ----------
    section, row, seat:
        Non-empty strings matched verbatim against the ``data-section``,
        ``data-row`` and ``data-seat`` attributes of seat ``<rect>``
        elements. Comparison is case-sensitive — Ticketmaster's seat-map
        SVG uses upper-case row labels, so callers should pre-normalise
        if they accept user input in mixed case.
    """

    def __init__(self, section: str, row: str, seat: str) -> None:
        if not section or not section.strip():
            raise ValueError("InteractiveSeatmapStrategy.section must be non-empty")
        if not row or not row.strip():
            raise ValueError("InteractiveSeatmapStrategy.row must be non-empty")
        if not seat or not seat.strip():
            raise ValueError("InteractiveSeatmapStrategy.seat must be non-empty")
        self.section = section
        self.row = row
        self.seat = seat

    # --- public API --------------------------------------------------------

    async def pick(self, page: Page) -> TicketCandidate | None:
        rect_selector = _rect_selector(self.section, self.row, self.seat)

        # 1. Try the top-level frame first.
        top_rect = page.locator(rect_selector).first
        if await _rect_present(top_rect):
            return await _click_and_build_candidate(
                top_rect, self.section, self.row, self.seat, where="top frame"
            )

        # 2. Fall back to every iframe matching ``seatmap_frame``.
        try:
            frame_selector_fallbacks = get_selectors("seatmap_frame")
        except KeyError:
            frame_selector_fallbacks = []
        for frame_selector in frame_selector_fallbacks:
            frame: FrameLocator = page.frame_locator(frame_selector)
            frame_rect = frame.locator(rect_selector).first
            if await _rect_present(frame_rect):
                return await _click_and_build_candidate(
                    frame_rect,
                    self.section,
                    self.row,
                    self.seat,
                    where=f"frame {frame_selector!r}",
                )

        log.warning(
            "Interactive seat-map: no rect matched section=%s row=%s seat=%s "
            "in the top frame or any of %d seatmap iframes",
            self.section,
            self.row,
            self.seat,
            len(frame_selector_fallbacks),
        )
        return None


# --- helpers --------------------------------------------------------------


def _rect_selector(section: str, row: str, seat: str) -> str:
    """Return the CSS selector for the seat-map ``<rect>`` carrying these attrs.

    Kept private so the data-attribute contract stays in one place. The
    strategy never compares the resulting selector to a YAML entry —
    seat coordinates are user input, not a registry lookup.
    """
    return (
        f'rect[data-section="{_css_escape_attr_value(section)}"]'
        f'[data-row="{_css_escape_attr_value(row)}"]'
        f'[data-seat="{_css_escape_attr_value(seat)}"]'
    )


def _css_escape_attr_value(value: str) -> str:
    """Escape ``"`` and ``\\`` so the value can sit inside a CSS attribute selector."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _rect_present(rect: Locator) -> bool:
    """Return ``True`` when the locator resolves to at least one element."""
    try:
        return await rect.count() > 0
    except Exception as exc:  # noqa: BLE001
        log.debug("Seat-map rect probe failed: %s", exc)
        return False


async def _click_and_build_candidate(
    rect: Locator,
    section: str,
    row: str,
    seat: str,
    *,
    where: str,
) -> TicketCandidate | None:
    """Click the resolved rect and wrap it in a :class:`TicketCandidate`.

    On click failure the strategy returns ``None`` rather than raising so
    the caller stays inside the standard ``pick() is None ⇒ try-again``
    control flow used by every other strategy.
    """
    try:
        await rect.scroll_into_view_if_needed(timeout=4000)
        await rect.click(timeout=4000)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Interactive seat-map: failed to click rect (%s, %s, %s) in %s: %s",
            section,
            row,
            seat,
            where,
            exc,
        )
        return None

    description = f"Seat-map pick: section={section} row={row} seat={seat} ({where})"
    log.info("Interactive seat-map clicked: %s", description)
    return TicketCandidate(
        locator=rect,
        price=None,
        section=section,
        row=row,
        description=description,
        sections=[section],
    )
