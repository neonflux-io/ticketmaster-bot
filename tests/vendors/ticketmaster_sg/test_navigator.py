"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.navigator`.

Drives the distilled SG fixtures from F7.1 plus a handful of small
synthetic ``page.set_content`` pages for state branches that the
fixtures don't already cover (sold-out, not-on-sale, queue redirect).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.vendors.ticketmaster_sg import navigator as sg_navigator
from src.vendors.ticketmaster_sg.navigator import (
    NavigationError,
    detect_state,
    open_event,
    parse_sg_date,
    wait_until_on_sale,
)

# ---------------------------------------------------------------------------
# Pure-Python SG date parser
# ---------------------------------------------------------------------------


def test_parse_sg_date_day_only() -> None:
    """``'10 Dec 2026'`` → midnight SGT on 10 Dec 2026."""
    dt = parse_sg_date("10 Dec 2026")
    assert dt.year == 2026
    assert dt.month == 12
    assert dt.day == 10
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.tzinfo is not None
    # SGT is UTC+08:00 with no DST → utcoffset() must be 8h.
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 8 * 3600


def test_parse_sg_date_full_format() -> None:
    """``'10 Dec 2026 (Thu.) 05:00 pm'`` → 17:00 SGT on 10 Dec 2026."""
    dt = parse_sg_date("10 Dec 2026 (Thu.) 05:00 pm")
    assert (dt.year, dt.month, dt.day) == (2026, 12, 10)
    assert (dt.hour, dt.minute) == (17, 0)


def test_parse_sg_date_full_format_no_weekday() -> None:
    """``'10 Dec 2026 05:00 pm'`` (no parenthesised weekday) parses too."""
    dt = parse_sg_date("10 Dec 2026 05:00 pm")
    assert (dt.hour, dt.minute) == (17, 0)


def test_parse_sg_date_morning_meridiem() -> None:
    """``'1 Jan 2027 12:00 am'`` → midnight (00:00) on 1 Jan 2027.

    SG 12-hour clock convention: 12 am is midnight, 12 pm is noon.
    """
    midnight = parse_sg_date("1 Jan 2027 12:00 am")
    noon = parse_sg_date("1 Jan 2027 12:00 pm")
    assert midnight.hour == 0
    assert noon.hour == 12


def test_parse_sg_date_rejects_iso() -> None:
    """ISO 8601 input is not an SG date — caller should use ``fromisoformat``."""
    with pytest.raises(ValueError):
        parse_sg_date("2026-12-10T17:00:00")


def test_parse_sg_date_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_sg_date("not a date")


# ---------------------------------------------------------------------------
# open_event - against the captured event-detail fixture
# ---------------------------------------------------------------------------


async def test_open_event_loads_real_event_detail_fixture(chromium_context, fixture_url) -> None:
    """The SG event-detail fixture is recognised as 'loaded' by open_event."""
    page = await chromium_context.new_page()
    url = fixture_url("vendors/ticketmaster_sg/event_detail.html")
    # Should not raise; the wait-for-selector inside open_event must
    # match the schedule table or date-list select.
    await open_event(page, url, timeout_seconds=5.0)
    # Sanity: the schedule table is actually present after the call.
    rows = page.locator("table.auto-game-list tbody tr[data-key]")
    assert await rows.count() >= 4


# ---------------------------------------------------------------------------
# wait_until_on_sale - rejects naive datetimes, accepts SG strings
# ---------------------------------------------------------------------------


async def test_wait_until_on_sale_rejects_naive_datetime(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.goto("about:blank")
    naive = datetime(2099, 1, 1, 0, 0, 0)
    with pytest.raises(NavigationError, match="timezone-aware"):
        await wait_until_on_sale(page, naive)


async def test_wait_until_on_sale_returns_when_past_on_sale_and_tickets_visible(
    chromium_context, fixture_url
) -> None:
    """SG ticket-area fixture exposes the quantity select → tickets available."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))
    # A date in the past so the function falls through to immediate polling.
    past = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    # The fixture has the autoMode button visible, so _tickets_available
    # is True on the first probe and the function returns without
    # reloading the page.
    await wait_until_on_sale(page, past, poll_deadline_seconds=5.0)


async def test_wait_until_on_sale_accepts_sg_date_string(chromium_context, fixture_url) -> None:
    """SG-format date string is parsed and waited on (past date → immediate)."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))
    # 1 Jan 2000 (well in the past) so we don't actually sleep.
    await wait_until_on_sale(
        page,
        "1 Jan 2000",
        poll_deadline_seconds=5.0,
    )


# ---------------------------------------------------------------------------
# detect_state - URL prefix rules
# ---------------------------------------------------------------------------


async def _serve_url(page, url: str, html: str = "<html><body>x</body></html>") -> None:
    """Intercept any HTTP request to ``url`` and return ``html``.

    Lets us drive ``page.goto(url)`` for an arbitrary synthetic URL so
    ``detect_state`` sees the real ``page.url`` prefix we want to test.
    """

    async def _handler(route):  # noqa: ANN001 - playwright type
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=html,
        )

    await page.route("**/*", _handler)


async def test_detect_state_on_check_captcha_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://ticketmaster.sg/ticket/check-captcha/26sg_pglcs2major/3239/1/21"
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "captcha"


async def test_detect_state_on_ticket_area_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://ticketmaster.sg/ticket/area/26sg_pglcs2major/3239"
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "tickets"


async def test_detect_state_on_activity_detail_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "event_detail"


async def test_detect_state_on_queue_it_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://ticketmasterasia.queue-it.net/?c=ticketmasterasia&e=foo"
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "queue"


async def test_detect_state_on_oauth_login_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = (
        "https://auth.ticketmaster.com/as/authorization.oauth2"
        "?client_id=1a554b2c04dc.web.ticketmaster.sg"
    )
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "login_required"


async def test_detect_state_on_identity_exchange_url(chromium_context) -> None:
    page = await chromium_context.new_page()
    target = "https://identity.ticketmaster.sg/exchange?code=abc&state=xyz"
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "login_in_progress"


async def test_detect_state_on_select_seat_url(chromium_context) -> None:
    """URL-prefix rule for the SG interactive seat-map step (F8.2)."""
    page = await chromium_context.new_page()
    target = "https://ticketmaster.sg/ticket/select-seat/26sg_sgopen2026/3318/46/88"
    await _serve_url(page, target)
    await page.goto(target)
    assert await detect_state(page) == "interactive_seatmap"


async def test_detect_state_iframe_with_select_seat_url(chromium_context) -> None:
    """Iframe-presence fallback: the live SG seat-map iframe loads the
    /ticket/select-seat/ URL while the parent stays on /ticket/area/...,
    so the navigator must climb into ``page.frames`` and match there
    too. Mirrors the F8.1 recon finding that ``page.url`` alone misses
    the Fancybox seat-map transition.
    """
    page = await chromium_context.new_page()
    parent_url = "https://ticketmaster.sg/ticket/area/26sg_sgopen2026/3318"
    iframe_url = "https://ticketmaster.sg/ticket/select-seat/26sg_sgopen2026/3318/46/88"

    async def _handler(route):  # noqa: ANN001 - Playwright Route is untyped
        if route.request.url.startswith(iframe_url):
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<html><body><table class='seat'></table></body></html>",
            )
            return
        if route.request.url == parent_url:
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=(
                    f"<!doctype html><html><body><iframe src='{iframe_url}'></iframe></body></html>"
                ),
            )
            return
        await route.fulfill(status=200, body="")

    await page.route("**/*", _handler)
    await page.goto(parent_url)
    # Wait for the iframe to attach so page.frames contains it.
    await page.wait_for_selector("iframe[src*='/ticket/select-seat/']")
    assert await detect_state(page) == "interactive_seatmap"


# ---------------------------------------------------------------------------
# detect_state - DOM marker fallbacks
# ---------------------------------------------------------------------------


async def test_detect_state_sold_out_marker(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.set_content("<!doctype html><html><body><main>SOLD OUT</main></body></html>")
    assert await detect_state(page) == "sold_out"


async def test_detect_state_not_on_sale_marker(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.set_content(
        "<!doctype html><html><body><main>Sale Starts 10 Dec 2026</main></body></html>"
    )
    assert await detect_state(page) == "not_on_sale"


async def test_detect_state_unknown_on_blank_page(chromium_context) -> None:
    page = await chromium_context.new_page()
    await page.set_content(
        "<!doctype html><html><body><main role='main'><h1>Hello</h1></main></body></html>"
    )
    assert await detect_state(page) == "unknown"


# ---------------------------------------------------------------------------
# detect_state - real ticket-area fixture should classify as "tickets"
# ---------------------------------------------------------------------------


async def test_detect_state_tickets_on_real_ticket_area_fixture(
    chromium_context, fixture_url
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/ticket_area.html"))
    # The fixture is a file:// URL → URL rules don't match. The
    # fallback _tickets_available probe sees autoMode + quantity select.
    assert await detect_state(page) == "tickets"


def test_module_exports() -> None:
    """Public surface is the four-symbol contract."""
    assert {
        "NavigationError",
        "detect_state",
        "open_event",
        "parse_sg_date",
        "wait_until_on_sale",
    } <= set(sg_navigator.__all__)
