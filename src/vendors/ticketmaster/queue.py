"""Queue handler - waits through Ticketmaster's virtual waiting room."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ...registry import selectors as selector_registry

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class QueueTimeoutError(Exception):
    """Raised when queue wait exceeds the configured max."""


async def in_queue(page: Page) -> bool:
    """Return True if the current page is showing the queue/waiting room."""
    url = page.url.lower()
    if "queue" in url or "waitingroom" in url:
        return True
    for frame in page.frames:
        if frame.url and ("queue" in frame.url.lower() or "waitingroom" in frame.url.lower()):
            return True
    try:
        body = await page.locator("body").inner_text(timeout=2000)
        lowered = body.lower()
        if (
            "waiting room" in lowered
            or "your place in line" in lowered
            or "you're now in line" in lowered
        ):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def wait_through_queue(
    page: Page,
    *,
    check_interval_seconds: float = 5.0,
    max_wait_seconds: float = 3600.0,
) -> None:
    """Poll the queue page until we are released to the event page."""
    log.info("Queue detected - waiting to be released...")

    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    last_position_log = 0.0

    while asyncio.get_event_loop().time() < deadline:
        still_queued = await in_queue(page)
        if not still_queued:
            log.info("Released from queue!")
            return

        now = asyncio.get_event_loop().time()
        if now - last_position_log > 30:
            position = await _try_read_queue_position(page)
            if position:
                log.info("Queue status: %s", position)
            else:
                log.info("Still in queue...")
            last_position_log = now

        await asyncio.sleep(check_interval_seconds)

    raise QueueTimeoutError("Queue wait exceeded maximum runtime")


async def _try_read_queue_position(page: Page) -> str | None:
    """Best-effort attempt to read queue position text."""
    selectors = selector_registry.get("queue_position")
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                text = await loc.inner_text(timeout=500)
                if text:
                    return text.strip()[:200]
        except Exception:  # noqa: BLE001
            continue
    for frame in page.frames:
        if "queue" not in (frame.url or "").lower():
            continue
        try:
            body = await frame.locator("body").inner_text(timeout=1000)
            for line in body.splitlines():
                low = line.lower()
                if "position" in low or "ahead of you" in low or "place in line" in low:
                    return line.strip()[:200]
        except Exception:  # noqa: BLE001
            continue
    return None
