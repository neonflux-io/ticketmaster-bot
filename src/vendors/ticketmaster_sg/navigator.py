"""ticketmaster.sg navigator: event open, on-sale polling, page-state detect.

SG-specific differences (versus :mod:`src.vendors.ticketmaster.navigator`):

* :func:`open_event` understands ``/activity/detail/<gameCode>`` URLs and
  treats both the schedule-table and the ``#date-list`` ``<select>`` as
  valid "loaded" markers.
* :func:`wait_until_on_sale` accepts SG-format on-sale strings
  (``"10 Dec 2026"`` or ``"10 Dec 2026 05:00 pm"``) in addition to the
  ISO-8601 strings the US flow uses. The parser is intentionally
  conservative — every accepted format is round-tripped to a
  timezone-aware :class:`datetime` so callers see one shape only.
* :func:`detect_state` recognises the SG URL prefixes documented in
  ``docs/recon/ticketmaster_sg.md`` (``/ticket/area/``,
  ``/ticket/check-captcha/``, ``identity.ticketmaster.sg``,
  ``ticketmasterasia.queue-it.net`` …) before falling back to the
  generic sold-out / not-on-sale text markers.

Like the US navigator, every DOM lookup goes through the
:mod:`src.vendors.ticketmaster_sg.selectors` helper so the
inline-selector grep gate stays green.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...utils.retry import random_human_delay
from .selectors import get as sg_selector_get
from .selectors import try_selector_for

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class NavigationError(Exception):
    """Raised when SG navigation fails irrecoverably."""


# ---------------------------------------------------------------------------
# SG date parsing
# ---------------------------------------------------------------------------

# Matches "10 Dec 2026" or "10 Dec 2026 05:00 pm" — the formats the SG
# event-detail and ticket-area pages render show times in.
_SG_DATE_RE = re.compile(
    r"""
    ^\s*
    (?P<day>\d{1,2})
    \s+
    (?P<month>[A-Za-z]{3,9})
    \s+
    (?P<year>\d{4})
    (?:
        \s+
        (?:\([A-Za-z]{2,4}\.\)\s+)?     # optional parenthesised weekday
        (?P<hour>\d{1,2})
        :
        (?P<minute>\d{2})
        \s+
        (?P<meridiem>am|pm)
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SG_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def parse_sg_date(value: str, *, tz: str = "Asia/Singapore") -> datetime:
    """Parse an SG-format on-sale string into a timezone-aware ``datetime``.

    Accepted shapes (case-insensitive):

    * ``"10 Dec 2026"`` — interpreted as 00:00 SGT.
    * ``"10 Dec 2026 (Thu.) 05:00 pm"`` — full event-detail layout.
    * ``"10 Dec 2026 05:00 pm"`` — without the parenthesised weekday.

    Raises :class:`ValueError` for any other shape; callers that want
    ISO-8601 should continue to use :func:`datetime.fromisoformat`.

    The ``tz`` parameter defaults to ``Asia/Singapore`` because the SG
    site renders every show time in local Singapore time; that maps to
    a fixed UTC+08:00 offset (no DST) so this is unambiguous.
    """
    if not isinstance(value, str):
        raise ValueError(f"parse_sg_date: expected str, got {type(value).__name__}")

    match = _SG_DATE_RE.match(value)
    if not match:
        raise ValueError(f"Not a recognised SG date string: {value!r}")
    day = int(match.group("day"))
    month_str = match.group("month").lower()
    if month_str not in _SG_MONTHS:
        raise ValueError(f"Unknown SG month abbreviation in {value!r}")
    month = _SG_MONTHS[month_str]
    year = int(match.group("year"))

    hour = int(match.group("hour") or 0)
    minute = int(match.group("minute") or 0)
    meridiem = match.group("meridiem")
    if meridiem is not None:
        if not (1 <= hour <= 12):
            raise ValueError(f"SG date {value!r} has invalid hour {hour}")
        if meridiem.lower() == "am":
            hour = 0 if hour == 12 else hour
        else:  # pm
            hour = hour if hour == 12 else hour + 12

    # Resolve the timezone. Asia/Singapore is UTC+08:00 fixed.
    from datetime import timedelta, tzinfo as _Tz

    tzinfo: _Tz
    try:
        from zoneinfo import ZoneInfo

        tzinfo = ZoneInfo(tz)
    except Exception:
        # Fall back to a fixed UTC+08:00 offset; Asia/Singapore has no
        # DST so the two are equivalent for our needs.
        tzinfo = timezone(timedelta(hours=8))

    return datetime(year, month, day, hour, minute, tzinfo=tzinfo)


# ---------------------------------------------------------------------------
# Page navigation + on-sale wait
# ---------------------------------------------------------------------------

# Selector that marks the event-detail page as "loaded enough" to act on.
_EVENT_DETAIL_MARKER_NAMES = (
    "event_date_row",
    "event_date_select",
    "event_find_tickets_link",
)


async def open_event(
    page: Page,
    url: str,
    *,
    timeout_seconds: float = 30,
) -> None:
    """Open an SG ``/activity/detail/<gameCode>`` URL.

    Tries each event-detail loaded-marker selector in turn so that an
    SG event-detail page is considered "ready" as soon as either the
    schedule table or the date-list ``<select>`` is mounted.
    """
    log.info("Opening SG event page: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))

    # The SG site sometimes embeds the cookie-consent banner ahead of
    # the schedule table; wait for any of the loaded markers.
    last_exc: Exception | None = None
    deadline_ms = int(timeout_seconds * 1000)
    for name in _EVENT_DETAIL_MARKER_NAMES:
        sel = try_selector_for(name)
        if sel is None:
            continue
        try:
            await page.wait_for_selector(sel, timeout=max(2000, deadline_ms // 2))
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    log.warning(
        "Could not find any SG event-detail loaded markers; continuing (last: %s)",
        last_exc,
    )


async def wait_until_on_sale(
    page: Page,
    on_sale_time: datetime | str | None,
    *,
    refresh_interval_seconds: float = 2.0,
    pre_sale_buffer_seconds: float = 30.0,
    poll_deadline_seconds: float = 1800.0,
) -> None:
    """Block until the SG event is on sale.

    Accepts either a ``datetime`` (must be timezone-aware) or a string
    in the SG event-detail format (e.g. ``"10 Dec 2026 (Thu.) 05:00 pm"``
    or simply ``"10 Dec 2026"``). The SG date string is parsed via
    :func:`parse_sg_date`, which always returns an SGT-localised
    ``datetime``.
    """
    target: datetime | None
    if isinstance(on_sale_time, str):
        target = parse_sg_date(on_sale_time)
    else:
        target = on_sale_time

    if target is not None:
        if target.tzinfo is None:
            raise NavigationError(
                "on_sale_time must be timezone-aware "
                "(use parse_sg_date or pass an ISO 8601 string with offset)"
            )
        now = datetime.now(tz=timezone.utc)
        seconds_until = (target - now).total_seconds()
        if seconds_until > pre_sale_buffer_seconds:
            wait_seconds = seconds_until - pre_sale_buffer_seconds
            log.info(
                "Sleeping %.1fs until %.0fs before SG on-sale time (%s)",
                wait_seconds,
                pre_sale_buffer_seconds,
                target.isoformat(),
            )
            await asyncio.sleep(wait_seconds)
        log.info("Approaching SG on-sale time, polling page...")

    poll_deadline = asyncio.get_event_loop().time() + max(60.0, poll_deadline_seconds)
    while asyncio.get_event_loop().time() < poll_deadline:
        if await _tickets_available(page):
            log.info("SG event tickets available")
            return
        log.debug("SG tickets not yet available - refreshing...")
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            log.debug("SG reload failed: %s", exc)
        await random_human_delay(refresh_interval_seconds, refresh_interval_seconds * 1.5)

    raise NavigationError("SG tickets did not become available within polling window")


async def _tickets_available(page: Page) -> bool:
    """Return True if either the schedule rows or the ticket-area form is shown."""
    for name in (
        "event_find_tickets_link",
        "event_date_row",
        "ticket_area_best_available_button",
        "ticket_area_quantity_select",
    ):
        sel = try_selector_for(name)
        if sel is None:
            continue
        try:
            count = await page.locator(sel).count()
            if count > 0 and await page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# State detector
# ---------------------------------------------------------------------------


# URL-prefix → state mapping. Ordered: queue first (highest priority),
# then explicit SG checkout / captcha gates, then the broader area /
# detail prefixes.
_URL_RULES: tuple[tuple[str, str], ...] = (
    ("queue-it.net", "queue"),
    ("/queue", "queue"),
    ("/waitingroom", "queue"),
    ("identity.ticketmaster.sg/exchange", "login_in_progress"),
    ("auth.ticketmaster.com/as/authorization.oauth2", "login_required"),
    ("/ticket/check-captcha/", "captcha"),
    ("/ticket/checkout/", "checkout"),
    ("/ticket/select-seat/", "interactive_seatmap"),
    ("/ticket/area/", "tickets"),
    ("/activity/detail/", "event_detail"),
)


async def detect_state(page: Page) -> str:
    """Classify the current SG page state.

    Returns one of: ``queue``, ``login_required``, ``login_in_progress``,
    ``captcha``, ``checkout``, ``interactive_seatmap``, ``tickets``,
    ``event_detail``, ``not_on_sale``, ``sold_out``, ``unknown``.
    """
    url = (page.url or "").lower()
    for prefix, state in _URL_RULES:
        if prefix in url:
            return state

    # Frame URLs (queue-it, captcha) can hide inside an iframe even
    # though the top URL is on ticketmaster.sg.
    for frame in page.frames:
        furl = (frame.url or "").lower()
        if "queue-it.net" in furl or "/queue" in furl:
            return "queue"

    # DOM-text fallbacks for sold-out / not-on-sale. The SG selectors
    # are joined and matched against ``main``, with a fallback to the
    # full page body if ``main`` is missing.
    main_sel = try_selector_for("main_content")
    scope = page
    if main_sel is not None:
        try:
            main = page.locator(main_sel).first
            if await main.count() > 0:
                scope = main  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            pass

    for sel in sg_selector_get("sold_out_marker"):
        try:
            if await scope.locator(sel).first.is_visible(timeout=400):
                return "sold_out"
        except Exception:  # noqa: BLE001
            continue

    for sel in sg_selector_get("not_on_sale_marker"):
        try:
            if await scope.locator(sel).first.is_visible(timeout=400):
                return "not_on_sale"
        except Exception:  # noqa: BLE001
            continue

    if await _tickets_available(page):
        return "tickets"
    return "unknown"


__all__ = [
    "NavigationError",
    "detect_state",
    "open_event",
    "parse_sg_date",
    "wait_until_on_sale",
]
