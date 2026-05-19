"""Navigator - event page loading, on-sale detection, auto-refresh."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..utils.retry import random_human_delay

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger("ticketmaster-bot")


class NavigationError(Exception):
    """Raised when navigation fails irrecoverably."""


async def open_event(
    page: Page,
    url: str,
    *,
    timeout_seconds: float = 30,
) -> None:
    """Open the event page and wait for primary content to render."""
    log.info("Opening event page: %s", url)
    await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_seconds * 1000))
    try:
        await page.wait_for_selector(
            "[data-bdd='quick-picks-list'], [data-bdd='event-onsale-time'], "
            "iframe[title*='queue' i], button:has-text('Find Tickets'), "
            "text=/On Sale/i, text=/Sale Starts/i",
            timeout=int(timeout_seconds * 1000),
        )
    except Exception:  # noqa: BLE001
        log.warning("Could not find expected event-page elements; continuing anyway")


async def wait_until_on_sale(
    page: Page,
    on_sale_time: datetime | None,
    *,
    refresh_interval_seconds: float = 2.0,
    pre_sale_buffer_seconds: float = 30.0,
    poll_deadline_seconds: float = 1800.0,
) -> None:
    """Block until the configured on-sale time, then start refresh polling.

    If on_sale_time is None, this just checks once and returns.
    """
    if on_sale_time is not None:
        if on_sale_time.tzinfo is None:
            raise NavigationError(
                "on_sale_time must be timezone-aware (config loader should ensure this)"
            )
        now = datetime.now(tz=timezone.utc)
        seconds_until = (on_sale_time - now).total_seconds()
        if seconds_until > pre_sale_buffer_seconds:
            wait_seconds = seconds_until - pre_sale_buffer_seconds
            log.info(
                "Sleeping %.1fs until %.0fs before on-sale time (%s)",
                wait_seconds,
                pre_sale_buffer_seconds,
                on_sale_time.isoformat(),
            )
            await asyncio.sleep(wait_seconds)
        log.info("Approaching on-sale time, polling page...")

    poll_deadline = asyncio.get_event_loop().time() + max(60.0, poll_deadline_seconds)
    while asyncio.get_event_loop().time() < poll_deadline:
        if await _tickets_available(page):
            log.info("Tickets available on page")
            return
        log.debug("Tickets not yet available - refreshing...")
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            log.debug("Reload failed: %s", exc)
        await random_human_delay(refresh_interval_seconds, refresh_interval_seconds * 1.5)

    raise NavigationError("Tickets did not become available within polling window")


async def _tickets_available(page: Page) -> bool:
    """Heuristic: tickets are on sale if quick-picks or ticket list is visible."""
    try:
        for selector in (
            "[data-bdd='quick-picks-list']",
            "[data-bdd='ticket-list']",
            "button:has-text('Find Tickets')",
            ".quick-picks",
        ):
            if await page.locator(selector).first.is_visible(timeout=500):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def detect_state(page: Page) -> str:
    """Classify the current page state.

    Returns one of: "queue", "tickets", "not_on_sale", "sold_out", "unknown".
    """
    url = page.url
    if "queue" in url.lower() or "waitingroom" in url.lower():
        return "queue"

    for frame in page.frames:
        if frame.url and (
            "queue" in frame.url.lower() or "waitingroom" in frame.url.lower()
        ):
            return "queue"

    main = page.locator("main, [role='main']").first
    try:
        if await main.count() == 0:
            scope = page
        else:
            scope = main  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        scope = page  # type: ignore[assignment]

    sold_out_selectors = (
        "text=/sold out/i",
        "text=/no tickets are available/i",
        "[data-bdd='sold-out']",
    )
    for sel in sold_out_selectors:
        try:
            if await scope.locator(sel).first.is_visible(timeout=400):
                return "sold_out"
        except Exception:  # noqa: BLE001
            continue

    not_on_sale_selectors = (
        "text=/sale starts/i",
        "text=/on sale .* at/i",
        "text=/presale/i",
        "[data-bdd='event-onsale-time']",
    )
    for sel in not_on_sale_selectors:
        try:
            if await scope.locator(sel).first.is_visible(timeout=400):
                return "not_on_sale"
        except Exception:  # noqa: BLE001
            continue

    if await _tickets_available(page):
        return "tickets"
    return "unknown"
