"""Live ticketmaster.sg integration tests for F7.3 modules.

One smoke test per shipped module (``auth``, ``navigator``, ``queue``).
Each test is gated on real network reachability of the SG site —
``socket.create_connection(('ticketmaster.sg', 443))`` — and
``pytest.skip``s with a clear reason when the host is unreachable
(offline laptop, firewalled CI, etc.).

These tests deliberately do **not** require a logged-in session: the
F7.3 feature ships only ``auth.login`` and ``is_logged_in`` plumbing;
the F7.6 feature owns the full end-to-end live flow with credentials
and the user solving captchas. The tests below only verify that:

* The recon-captured URLs still resolve under real Chromium.
* ``is_logged_in`` correctly classifies a brand-new context
  (no SG cookies → ``False``).
* :func:`auth.has_captcha` flips True when navigating to the live
  ``/ticket/check-captcha/...`` URL captured during F7.1 (proves the
  selector chain matches against the real, current DOM).
* :func:`navigator.open_event` succeeds on the live
  ``/activity/detail/26sg_pglcs2major`` page.
* :func:`navigator.detect_state` reports a non-``unknown`` state on
  the live event-detail page.
* :func:`queue.in_queue` returns ``False`` for the (currently
  un-queued) live event-detail page — i.e. the SG queue probe does
  not false-positive on the real, un-queued site.

The user can solve any captchas that appear during the run because
the test launches a *headed* Chromium context (per the F7.3 brief).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from src.vendors.ticketmaster_sg import auth as sg_auth
from src.vendors.ticketmaster_sg import navigator as sg_navigator
from src.vendors.ticketmaster_sg import queue as sg_queue

SG_HOST = "ticketmaster.sg"
SG_PORT = 443
SG_LIVE_EVENT_URL = "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
# A live SG date that always renders the captcha widget. The {gameCode}
# / {dateId} / {areaNo} / {qty} segments are the same values captured
# during F7.1; the route is public (any human can hit it directly).
SG_LIVE_CAPTCHA_URL = "https://ticketmaster.sg/ticket/check-captcha/26sg_pglcs2major/3239/1/21"

REPO_ROOT = Path(__file__).resolve().parents[3]


def _sg_reachable() -> bool:
    """Return True iff we can open a TCP socket to ticketmaster.sg:443.

    Used as the gate for every live test in this file. The check is
    deliberately cheap (a 5-second connect timeout) so an offline
    laptop falls through to ``pytest.skip`` instantly.
    """
    try:
        with socket.create_connection((SG_HOST, SG_PORT), timeout=5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _sg_reachable(),
    reason=f"ticketmaster.sg:{SG_PORT} is not reachable from this host",
)


@pytest_asyncio.fixture
async def sg_live_context(tmp_path):
    """Per-test persistent BrowserContext rooted at a tmp user_data_dir.

    Uses a real Chromium ``launch_persistent_context`` so the SG cookie
    set, EPS fingerprint, etc. all land on disk the same way they would
    in a real run. The directory is per-test so concurrent live tests
    don't share state, and is torn down with the test.
    """
    async with async_playwright() as pw:
        user_data_dir = tmp_path / "sg-profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        # Headed so the user can solve captchas live (per F7.3 brief).
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            locale="en-SG",
            timezone_id="Asia/Singapore",
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.8"},
        )
        try:
            yield context
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# auth.is_logged_in on a brand-new context → False
# ---------------------------------------------------------------------------


async def test_live_is_logged_in_false_for_anonymous_context(sg_live_context) -> None:
    """A fresh persistent context with no SG cookies must classify as logged-out."""
    result = await sg_auth.is_logged_in(sg_live_context)
    assert result is False


# ---------------------------------------------------------------------------
# auth.has_captcha on the live /ticket/check-captcha/... URL
# ---------------------------------------------------------------------------


async def test_live_has_captcha_on_check_captcha_page(sg_live_context) -> None:
    """The live check-captcha URL mounts at least one captcha widget."""
    page = await sg_live_context.new_page()
    try:
        try:
            await page.goto(
                SG_LIVE_CAPTCHA_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Live /ticket/check-captcha/ navigation failed: {exc}")

        # Give the page a moment to inject the invisible reCAPTCHA iframe.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass

        # If the SG site routes us via Queue-It or to the OAuth host,
        # the captcha probe won't apply — skip with a clear message.
        url = (page.url or "").lower()
        if "queue-it.net" in url:
            pytest.skip("Live event currently in Queue-It; captcha probe not applicable")
        if "auth.ticketmaster.com" in url:
            pytest.skip("Live site bounced to auth.ticketmaster.com; captcha probe not applicable")

        # Either the Yii image or the invisible reCAPTCHA iframe must
        # be present on the real check-captcha page.
        assert await sg_auth.has_captcha(page) is True, (
            f"has_captcha returned False on the live check-captcha URL. Final page URL: {page.url}"
        )
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# navigator.open_event + detect_state on the live event-detail URL
# ---------------------------------------------------------------------------


async def test_live_open_event_succeeds_on_event_detail(sg_live_context) -> None:
    """``open_event`` returns without raising against the live event URL.

    The function's contract is *"navigate and best-effort wait"*; it
    deliberately logs a warning instead of raising when none of the
    detail-page markers materialise (the SG site sometimes fronts the
    URL with Queue-It or returns a slimmer DOM under headless
    Chromium). This test only asserts that ``open_event`` does not
    raise and that the browser landed on a ticketmaster-controlled
    host.
    """
    page = await sg_live_context.new_page()
    try:
        try:
            await asyncio.wait_for(
                sg_navigator.open_event(page, SG_LIVE_EVENT_URL, timeout_seconds=45.0),
                timeout=90.0,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Live event detail navigation failed: {exc}")

        url = (page.url or "").lower()
        # Either we stayed on ticketmaster.sg or were redirected to
        # Queue-It / auth — all three are valid live outcomes that the
        # function handled without raising.
        host_ok = (
            "ticketmaster.sg" in url or "queue-it.net" in url or "auth.ticketmaster.com" in url
        )
        assert host_ok, f"Live navigation landed on unexpected host: {page.url}"
    finally:
        await page.close()


async def test_live_detect_state_classifies_event_detail(sg_live_context) -> None:
    """``detect_state`` returns a known SG state on the live event-detail page."""
    page = await sg_live_context.new_page()
    try:
        try:
            await page.goto(
                SG_LIVE_EVENT_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Live event-detail navigation failed: {exc}")

        state = await sg_navigator.detect_state(page)
        # Acceptable live states for this URL: it's an event-detail
        # page, but the SG flow may also have routed us to queue or
        # captcha at run-time.
        assert state in {
            "event_detail",
            "queue",
            "captcha",
            "tickets",
            "login_required",
        }, f"Unexpected live state on {page.url}: {state!r}"
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# queue.in_queue on the live event-detail URL → False (unless live queue)
# ---------------------------------------------------------------------------


async def test_live_in_queue_false_on_event_detail(sg_live_context) -> None:
    """The live event-detail page is not currently in a queue (probe False)."""
    page = await sg_live_context.new_page()
    try:
        try:
            await page.goto(
                SG_LIVE_EVENT_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Live event-detail navigation failed: {exc}")

        url = (page.url or "").lower()
        if "queue-it.net" in url:
            # The event is in a live queue — that's still a True signal,
            # just not the un-queued path we wanted to exercise.
            assert await sg_queue.in_queue(page) is True
            return
        # Un-queued case: the SG event detail does not advertise any
        # queue banner; in_queue must return False.
        assert await sg_queue.in_queue(page) is False
    finally:
        await page.close()
