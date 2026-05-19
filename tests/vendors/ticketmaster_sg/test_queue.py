"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.queue`.

Exercises the SG queue handler against synthetic pages that mimic
the three real signals the SG site emits when in a queue:

1. A top-level URL on ``*.queue-it.net``.
2. An in-line queue banner (``"your place in line"`` text on the SG
   page itself) — observed by F7.1 recon as the light-queue fallback.
3. A frame URL on ``*.queue-it.net`` (e.g. embedded iframe).
"""

from __future__ import annotations

import asyncio

import pytest

from src.vendors.ticketmaster_sg import queue as sg_queue


async def _serve(page, url: str, html: str) -> None:
    """Route ``url`` to a synthetic HTML body."""

    async def _handler(route):  # noqa: ANN001 - playwright type
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=html,
        )

    await page.route("**/*", _handler)


# ---------------------------------------------------------------------------
# in_queue - URL-based detection
# ---------------------------------------------------------------------------


async def test_in_queue_true_on_queue_it_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://ticketmasterasia.queue-it.net/?c=ticketmasterasia&e=foo"
    await _serve(page, target, "<html><body>You're in line</body></html>")
    await page.goto(target)
    assert await sg_queue.in_queue(page) is True


async def test_in_queue_false_on_sg_event_page(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    assert await sg_queue.in_queue(page) is False


# ---------------------------------------------------------------------------
# in_queue - body-text fallback for light queues
# ---------------------------------------------------------------------------


async def test_in_queue_true_on_inline_queue_banner(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.set_content(
        "<!doctype html><html><body><main>Your place in line: 1,234</main></body></html>"
    )
    assert await sg_queue.in_queue(page) is True


async def test_in_queue_true_on_people_ahead_marker(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.set_content(
        "<!doctype html><html><body>"
        "<div class='queue-position'>5,000 people ahead of you</div>"
        "</body></html>"
    )
    assert await sg_queue.in_queue(page) is True


# ---------------------------------------------------------------------------
# in_queue - real ticket-area fixture is NOT in queue
# ---------------------------------------------------------------------------


async def test_in_queue_false_on_ticket_area_fixture(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))
    assert await sg_queue.in_queue(page) is False


# ---------------------------------------------------------------------------
# wait_through_queue - returns immediately when not in queue
# ---------------------------------------------------------------------------


async def test_wait_through_queue_returns_when_not_queued(chromium_context, fixture_url) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    # Not in queue → first probe returns False → function returns instantly.
    # Use a tight max_wait so a regression here surfaces fast.
    await asyncio.wait_for(
        sg_queue.wait_through_queue(page, check_interval_seconds=0.1, max_wait_seconds=5.0),
        timeout=8.0,
    )


# ---------------------------------------------------------------------------
# wait_through_queue - times out if perpetually queued
# ---------------------------------------------------------------------------


async def test_wait_through_queue_raises_on_timeout(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://ticketmasterasia.queue-it.net/?c=ticketmasterasia&e=foo"
    await _serve(page, target, "<html><body>Your place in line: 12,345</body></html>")
    await page.goto(target)
    with pytest.raises(sg_queue.QueueTimeoutError):
        await sg_queue.wait_through_queue(
            page,
            check_interval_seconds=0.1,
            max_wait_seconds=0.5,
        )


# ---------------------------------------------------------------------------
# _try_read_queue_position picks up an inline banner
# ---------------------------------------------------------------------------


async def test_try_read_queue_position_reads_inline_banner(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.set_content(
        "<!doctype html><html><body>"
        "<div class='queue-position'>Your place in line: 1,234</div>"
        "</body></html>"
    )
    text = await sg_queue._try_read_queue_position(page)
    assert text is not None
    assert "1,234" in text


def test_module_exports() -> None:
    assert {"QueueTimeoutError", "in_queue", "wait_through_queue"} <= set(sg_queue.__all__)
