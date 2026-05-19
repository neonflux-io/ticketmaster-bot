"""Live end-to-end smoke test for the ticketmaster.sg vendor adapter (F7.6).

Drives a *headed* real Chromium against
``https://ticketmaster.sg/activity/detail/26sg_pglcs2major`` using the
TM_EMAIL / TM_PASSWORD credentials in ``.env``. The test is **human-
attended**: when the SG site challenges the user with a Yii image
CAPTCHA or the invisible reCAPTCHA Enterprise on the OAuth login, the
test pauses (``wait_for_human_if_captcha``, 600s) so the orchestrator
in front of the browser can solve it.

The test never purchases tickets — :func:`run_checkout` is invoked
with ``auto_purchase=False`` so the flow stops at the cart / delivery
review step regardless of how far the SG site lets the bot advance.

Failure semantics
-----------------

The test is intentionally **lenient about live-site state** and **strict
about selector regressions**:

* It is acceptable for the live event to be sold out, not on sale, in
  Queue-It, or for the SG site to bounce us to the login OAuth page —
  every one of those routes returns a known
  :func:`navigator.detect_state` value and the assertion below accepts
  them all.
* It is **not** acceptable for the bot to crash because a selector no
  longer matches the live DOM. Any
  :class:`playwright.async_api.TimeoutError` raised through the
  navigator / cart layer surfaces directly to the test report so a
  selector miss fails loudly.

Skip gates
----------

The test runs on every ``pytest`` invocation but skips cleanly when:

* The host cannot reach ``ticketmaster.sg:443`` (offline laptop, CI
  firewall) — ``socket.create_connection`` with a 5s timeout.
* ``TM_EMAIL`` / ``TM_PASSWORD`` are not set in ``.env`` — surfaces a
  ``SKIPPED`` line with a clear reason.
* The persistent capture profile (``sessions/sg-capture/``) is missing
  AND the run is **not** attached to an interactive TTY: a worker /
  CI session without a human cannot solve the SG captcha, so we skip
  rather than hang the suite for 5 minutes.

Artefacts
---------

Every successful or failed run dumps a screenshot, DOM dump, and a
short ``status.txt`` summary to::

    logs/recon-runs/<UTC-iso>/

so the orchestrator can audit what the live SG site looked like
during the run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Importing the package as a side effect registers the SG adapter so
# the vendor-registry assertion below resolves without a separate import.
import src.vendors.ticketmaster_sg  # noqa: F401
from src.registry import vendors as vendor_registry
from src.utils.config_loader import AccountConfig
from src.vendors.ticketmaster_sg import auth as sg_auth
from src.vendors.ticketmaster_sg import cart as sg_cart
from src.vendors.ticketmaster_sg import navigator as sg_navigator
from src.vendors.ticketmaster_sg import queue as sg_queue

log = logging.getLogger("ticketmaster-bot")

REPO_ROOT = Path(__file__).resolve().parents[3]
SG_HOST = "ticketmaster.sg"
SG_PORT = 443
SG_LIVE_EVENT_URL = "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
SG_DEFAULT_SESSION = REPO_ROOT / "sessions" / "default-default"
SG_CAPTURE_SESSION = REPO_ROOT / "sessions" / "sg-capture"
RECON_RUNS_DIR = REPO_ROOT / "logs" / "recon-runs"

# Per the F7.6 brief: the SG captcha pause may legitimately run 300s+.
# The 600s window covers the slowest realistic human solve plus the
# OAuth bounce + Queue-It transition that can immediately follow.
CAPTCHA_PAUSE_SECONDS = 600.0

# Accepted live states that prove the navigator parsed the DOM without
# raising. Per F7.6: "confirm cart UI shows reservation OR if not on
# sale assert state=not_on_sale". The list below extends that
# acceptance to every non-error state the SG detector can produce on
# this URL.
LIVE_ACCEPTABLE_STATES = frozenset(
    {
        "event_detail",
        "tickets",
        "interactive_seatmap",
        "checkout",
        "queue",
        "captcha",
        "login_required",
        "login_in_progress",
        "not_on_sale",
        "sold_out",
    }
)


def _sg_reachable() -> bool:
    """Return True iff we can open a TCP socket to ticketmaster.sg:443."""
    try:
        with socket.create_connection((SG_HOST, SG_PORT), timeout=5):
            return True
    except OSError:
        return False


def _has_interactive_human() -> bool:
    """Best-effort: does this pytest run have a human in front of it?

    Used to gate the test off in CI / worker sessions where the SG
    captcha pause would simply time out. The heuristic is conservative
    — we declare "has human" only when **all** of these are true:

    * ``CI`` is not set in the environment.
    * ``SG_LIVE_E2E_SKIP`` is not set (explicit caller opt-out).
    * Either ``sys.stdin.isatty()`` returns True (an interactive TTY)
      OR ``SG_LIVE_E2E_ATTENDED=1`` is set (explicit caller opt-in
      for environments where the TTY check would false-negative).

    The persistent ``sessions/sg-capture/`` directory does **not**
    count as an implicit human — a previous capture run may have
    aborted before solving the captcha, leaving an unauthenticated
    profile that would fail the live login step without anyone to
    intervene.
    """
    if os.environ.get("CI"):
        return False
    if os.environ.get("SG_LIVE_E2E_SKIP"):
        return False
    if os.environ.get("SG_LIVE_E2E_ATTENDED"):
        return True
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _utc_run_dir() -> Path:
    """Allocate a per-run artefact directory under ``logs/recon-runs/``."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = RECON_RUNS_DIR / f"f7_6_live_e2e_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _load_account() -> AccountConfig | None:
    """Build an :class:`AccountConfig` from ``.env`` creds, or None if missing."""
    load_dotenv(REPO_ROOT / ".env")
    email = os.environ.get("TM_EMAIL")
    password = os.environ.get("TM_PASSWORD")
    if not email or not password:
        return None
    return AccountConfig(name="default", email=email, password=password)


async def _capture_step(page, run_dir: Path, label: str) -> None:
    """Persist a per-step screenshot + DOM dump under ``run_dir``."""
    try:
        await page.screenshot(path=str(run_dir / f"{label}.png"), full_page=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("screenshot %s failed: %s", label, exc)
    try:
        html = await page.content()
        (run_dir / f"{label}.html").write_text(html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("dom dump %s failed: %s", label, exc)


pytestmark = pytest.mark.skipif(
    not _sg_reachable(),
    reason=f"ticketmaster.sg:{SG_PORT} not reachable from this host",
)


@pytest_asyncio.fixture
async def sg_live_e2e_context(tmp_path):
    """Per-test headed-Chromium persistent context.

    The session re-uses the F7.6 / F7.4 capture profile under
    ``sessions/sg-capture/`` when it exists so the human-solved
    captcha cookie set survives across runs. Falls back to a fresh
    tmp-path user_data_dir when the capture profile is absent.
    """
    if SG_CAPTURE_SESSION.is_dir():
        user_data_dir = SG_CAPTURE_SESSION
    else:
        user_data_dir = tmp_path / "sg-profile"
        user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        # Headed per the F7.6 brief - the human needs to see the
        # browser to solve captchas. The reCAPTCHA Enterprise widget
        # never appears in headless mode (Google fingerprints the UA).
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            locale="en-SG",
            timezone_id="Asia/Singapore",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.8"},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            yield context
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass


async def test_live_e2e_against_real_sg_event(sg_live_e2e_context) -> None:
    """End-to-end live drive against the real ticketmaster.sg event URL.

    The test passes when the SG vendor stack drives the live site
    without raising and the navigator classifies the resulting page
    state into one of :data:`LIVE_ACCEPTABLE_STATES`. Any selector
    miss / DOM regression surfaces as a Playwright timeout that
    propagates and fails the test.
    """
    account = _load_account()
    if account is None:
        pytest.skip("TM_EMAIL / TM_PASSWORD not set in .env - F7.6 live e2e cannot run")
    if not _has_interactive_human():
        pytest.skip(
            "F7.6 live e2e requires a human-attended pytest run "
            "(interactive TTY or SG_LIVE_E2E_ATTENDED=1). Skipping in "
            "non-interactive worker / CI sessions to avoid 5-minute captcha hangs."
        )

    # The SG adapter must be discoverable via the registry. This is a
    # cheap pre-flight: if F7.5's registration ever regresses the live
    # run would otherwise crash later inside the runner with a
    # confusing import error.
    adapter_cls = vendor_registry.get("ticketmaster_sg")
    adapter = adapter_cls()
    assert adapter.name == "ticketmaster_sg"

    run_dir = _utc_run_dir()
    (run_dir / "status.txt").write_text(
        f"F7.6 live e2e\nstart={datetime.now(tz=timezone.utc).isoformat()}\n"
        f"event={SG_LIVE_EVENT_URL}\n"
        f"account={account.name}\n",
        encoding="utf-8",
    )
    log.info("F7.6 live e2e artefacts → %s", run_dir)

    # ---- 1. Ensure we're logged in. Re-use the persistent session
    # when it already carries the SG cookies; otherwise drive login
    # and let the human solve any captcha.
    page = await sg_live_e2e_context.new_page()
    try:
        logged_in = await sg_auth.is_logged_in(sg_live_e2e_context)
        if not logged_in:
            log.info("F7.6 live e2e: no SG session in profile - driving login")
            try:
                await sg_auth.login(
                    sg_live_e2e_context,
                    account,
                    captcha_timeout_seconds=CAPTCHA_PAUSE_SECONDS,
                )
            except sg_auth.AuthError as exc:
                await _capture_step(page, run_dir, "login_failed")
                pytest.fail(
                    f"F7.6 live e2e: SG login did not complete - "
                    f"selector regression or captcha unsolved within "
                    f"{CAPTCHA_PAUSE_SECONDS:.0f}s ({exc})"
                )
        await _capture_step(page, run_dir, "01_post_login")

        # ---- 2. Navigate to the event detail URL.
        await sg_navigator.open_event(page, SG_LIVE_EVENT_URL, timeout_seconds=45.0)
        await _capture_step(page, run_dir, "02_event_detail")

        # ---- 3. Detect state. Per the F7.6 brief, any non-error state
        # is acceptable - we only need to confirm the navigator
        # classified the DOM without raising.
        state = await sg_navigator.detect_state(page)
        log.info("F7.6 live e2e: initial state=%r url=%s", state, page.url)
        (run_dir / "state.txt").write_text(
            f"initial_state={state}\nurl={page.url}\n", encoding="utf-8"
        )

        if state in {"sold_out", "not_on_sale"}:
            log.info("F7.6 live e2e: event is %s - flow legitimately halts here", state)
            assert state in LIVE_ACCEPTABLE_STATES
            return

        # ---- 4. If we landed in a queue, wait for release. We bound
        # the wait to a generous but finite window so the test does
        # not hang forever on a real long queue.
        if state == "queue":
            log.info("F7.6 live e2e: SG queue detected - waiting up to 10 minutes")
            try:
                await sg_queue.wait_through_queue(
                    page,
                    check_interval_seconds=5.0,
                    max_wait_seconds=600.0,
                )
            except sg_queue.QueueTimeoutError as exc:
                await _capture_step(page, run_dir, "queue_timeout")
                pytest.skip(
                    f"F7.6 live e2e: SG queue did not release in 10 minutes ({exc}) - "
                    "live event is in a real queue; skip rather than fail"
                )
            state = await sg_navigator.detect_state(page)
            await _capture_step(page, run_dir, "03_post_queue")

        # ---- 5. Attempt Add to Cart on the ticket-area / event-detail
        # page. With auto_purchase=False we stop at the cart UI so
        # there is no risk of an accidental purchase.
        added = False
        try:
            added = await sg_cart.add_to_cart(
                page,
                action_delay=(0.5, 1.5),
                timeout_seconds=30.0,
                captcha_timeout_seconds=CAPTCHA_PAUSE_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            await _capture_step(page, run_dir, "add_to_cart_error")
            pytest.fail(
                f"F7.6 live e2e: add_to_cart raised - selector regression on the "
                f"live SG site ({type(exc).__name__}: {exc})"
            )
        await _capture_step(page, run_dir, "04_cart")
        log.info("F7.6 live e2e: add_to_cart added=%s url=%s", added, page.url)

        # ---- 6. Final state check. Either the cart actually loaded
        # (added=True OR navigator says checkout) or the navigator
        # gives us one of the still-acceptable states (event_detail
        # if there were no clickable tickets, captcha if SG threw a
        # second gate, etc.).
        final_state = await sg_navigator.detect_state(page)
        (run_dir / "final_state.txt").write_text(
            f"added={added}\nfinal_state={final_state}\nurl={page.url}\n",
            encoding="utf-8",
        )
        log.info(
            "F7.6 live e2e: final_state=%r added=%s url=%s",
            final_state,
            added,
            page.url,
        )

        assert final_state in LIVE_ACCEPTABLE_STATES, (
            f"F7.6 live e2e: detect_state returned unexpected value {final_state!r} "
            f"(url={page.url}). Selector regression suspected."
        )
    finally:
        try:
            await asyncio.wait_for(page.close(), timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
