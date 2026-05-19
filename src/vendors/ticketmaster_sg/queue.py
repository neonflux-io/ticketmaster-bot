"""ticketmaster.sg Queue-It handler.

The SG site fronts every page with the Queue-It JS interceptor
(``data-queueit-c="ticketmasterasia"`` — verified during F7.1 recon).
When TM enables queueing for an event, the browser is hard-redirected
to ``<event>.queue-it.net`` (a separate origin) and the user holds
there until released back to the SG site.

This module recognises both:

* The Queue-It hosted UI (``*.queue-it.net`` URL on the page or any
  frame).
* Any in-line queue banner the SG site occasionally renders for
  light queues (``"your place in line"``, ``"people ahead of you"``…),
  matched via the ``queue_position_text`` selector group from
  ``config/selectors/ticketmaster_sg.yaml``.

It also keeps the legacy URL/title detection from the US Ticketmaster
adapter (``"queue"``, ``"waitingroom"``) so the SG queue handler still
handles a future hand-off where the URL passes through a US-flavoured
``/queue/...`` path without going to Queue-It.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .selectors import get as sg_selector_get
from .selectors import try_selector_for

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class QueueTimeoutError(Exception):
    """Raised when the SG queue wait exceeds the configured maximum."""


_QUEUE_URL_MARKERS = (
    "queue-it.net",
    "ticketmasterasia.queue-it.net",
    "/queue",
    "/waitingroom",
)

_BODY_TEXT_MARKERS = (
    "waiting room",
    "your place in line",
    "you're now in line",
    "people ahead of you",
    "queue position",
)


def _url_in_queue(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(marker in lowered for marker in _QUEUE_URL_MARKERS)


async def in_queue(page: Page) -> bool:
    """Return True if the SG page is currently sitting in a queue.

    Detection rules, in order:

    1. The page URL or any frame URL matches one of the Queue-It /
       waiting-room markers.
    2. The page body contains a queue-status phrase (light-queue case).
    3. A registered ``queue_position_text`` selector matches.
    """
    if _url_in_queue(page.url):
        return True
    for frame in page.frames:
        if _url_in_queue(frame.url):
            return True

    body_sel = try_selector_for("page_body")
    if body_sel is not None:
        try:
            body = await page.locator(body_sel).first.inner_text(timeout=2000)
            lowered = (body or "").lower()
            for marker in _BODY_TEXT_MARKERS:
                if marker in lowered:
                    return True
        except Exception:  # noqa: BLE001
            pass

    for sel in sg_selector_get("queue_position_text"):
        try:
            if await page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def wait_through_queue(
    page: Page,
    *,
    check_interval_seconds: float = 5.0,
    max_wait_seconds: float = 3600.0,
) -> None:
    """Poll the queue page until released back to ticketmaster.sg.

    Logs a position read-out at most once every 30 seconds. The
    position text is read either from the SG-side ``queue_position_text``
    selector group or from inside the Queue-It frame body — whichever
    yields text first.
    """
    log.info("SG queue detected - waiting to be released...")

    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    last_position_log = 0.0

    while asyncio.get_event_loop().time() < deadline:
        if not await in_queue(page):
            log.info("Released from SG queue!")
            return

        now = asyncio.get_event_loop().time()
        if now - last_position_log > 30:
            position = await _try_read_queue_position(page)
            if position:
                log.info("SG queue status: %s", position)
            else:
                log.info("Still in SG queue...")
            last_position_log = now

        await asyncio.sleep(check_interval_seconds)

    raise QueueTimeoutError("SG queue wait exceeded maximum runtime")


async def _try_read_queue_position(page: Page) -> str | None:
    """Best-effort attempt to read SG queue position text."""
    # 1) SG-side selector(s) for an inline status banner.
    for sel in sg_selector_get("queue_position_text"):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                text = await loc.inner_text(timeout=500)
                if text and text.strip():
                    return text.strip()[:200]
        except Exception:  # noqa: BLE001
            continue

    # 2) The Queue-It hosted page renders the status inside its own
    #    document. Iterate the frame list and scrape the body of any
    #    frame whose URL contains "queue".
    body_sel = try_selector_for("page_body")
    if body_sel is None:
        return None

    for frame in page.frames:
        if not _url_in_queue(frame.url):
            continue
        try:
            body = await frame.locator(body_sel).first.inner_text(timeout=1000)
        except Exception:  # noqa: BLE001
            continue
        for line in (body or "").splitlines():
            low = line.lower()
            if (
                "position" in low
                or "ahead of you" in low
                or "place in line" in low
                or "estimated wait" in low
            ):
                return line.strip()[:200]
    return None


__all__ = [
    "QueueTimeoutError",
    "in_queue",
    "wait_through_queue",
]
