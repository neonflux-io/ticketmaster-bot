"""Live cart + checkout DOM capture for ticketmaster.sg — F7.4.

Drives a *headed* Chromium with the user's TM_EMAIL / TM_PASSWORD against
the real ticketmaster.sg site, pauses for the user to solve any
captcha that appears (Yii image-CAPTCHA on /ticket/check-captcha and
invisible reCAPTCHA Enterprise on the OAuth login), and dumps:

  tests/fixtures/vendors/ticketmaster_sg/cart.html
  tests/fixtures/vendors/ticketmaster_sg/checkout.html

The script is re-runnable; it uses a *persistent* user_data_dir under
``sessions/sg-capture/`` so the SG cookies survive across runs (one
successful captcha solve covers subsequent attempts during the same
day).

NOT a test. NOT imported by any test. Hand-run once during F7.4 to
populate the fixtures the F7.4 pytest cases load via file:// URLs.

Run:

    .venv/bin/python docs/recon/ticketmaster_sg/_cart_capture.py

This rewrite (post-F7.7 hand-run regression) keeps the same control
flow but adds *much* more verbose per-step logging so the operator can
see exactly which step is running, which selector is being awaited,
and — crucially — where the script is stuck when a URL-substring wait
loops silently. Every log line is prefixed ``[capture HH:MM:SS]``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"
SESSIONS_DIR = REPO_ROOT / "sessions" / "sg-capture"
SCREENSHOTS_DIR = REPO_ROOT / "docs" / "recon" / "ticketmaster_sg" / "f7_4_capture"

# Reach the SG selector registry so capture stays in lockstep with the
# vendor adapter's YAML — no hand-baked CSS in this script.
sys.path.insert(0, str(REPO_ROOT))
from src.vendors.ticketmaster_sg import selectors as sg_selectors  # noqa: E402

EVENT_URL = "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
HOME_URL = "https://ticketmaster.sg/"
PROFILE_URL = "https://ticketmaster.sg/profile"


# ----- observability ----------------------------------------------------

_RUN_STARTED_AT = time.monotonic()
_WRITTEN: list[tuple[Path, int]] = []
_SKIPPED: list[tuple[str, str]] = []


def vlog(*parts: object) -> None:
    """Verbose logger. Prefixes each line with ``[capture HH:MM:SS]``."""
    stamp = datetime.now().strftime("%H:%M:%S")
    msg = " ".join(str(p) for p in parts)
    sys.stdout.write(f"[capture {stamp}] {msg}\n")
    sys.stdout.flush()


def _print(msg: str) -> None:
    # Back-compat shim so old call-sites keep working but go through vlog.
    vlog(msg)


def _safe_url(page) -> str:
    try:
        return page.url or "<no-url>"
    except Exception as exc:  # noqa: BLE001
        return f"<url-error: {exc}>"


async def _snippet(page, *, limit: int = 500) -> str:
    try:
        html = await page.content()
    except Exception as exc:  # noqa: BLE001
        return f"<content-error: {exc}>"
    flat = " ".join(html.split())
    return flat[:limit]


async def _safe_wait_for(
    locator,
    *,
    name: str,
    state: str = "visible",
    timeout_ms: int = 15_000,
    page=None,
) -> bool:
    """Wrap ``locator.wait_for`` with before/after logging + diagnostic on failure."""
    vlog(
        f"selector-wait BEGIN name={name} state={state} timeout={timeout_ms}ms "
        f"url={_safe_url(page) if page is not None else '<n/a>'}"
    )
    try:
        await locator.wait_for(state=state, timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        vlog(f"selector-wait FAIL  name={name} reason={exc!r}")
        if page is not None:
            vlog(f"selector-wait FAIL  name={name} url-at-fail={_safe_url(page)}")
            snip = await _snippet(page)
            vlog(f"selector-wait FAIL  name={name} body-first-500={snip!r}")
        return False
    count = -1
    try:
        count = await locator.count()
    except Exception:  # noqa: BLE001
        pass
    vlog(f"selector-wait OK    name={name} matched_count={count}")
    return True


async def _wait_until(page, predicate, timeout_seconds: float, *, poll: float = 1.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            if predicate(page):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(poll)
    return False


async def _wait_for_url_substring(
    page,
    substrings: tuple[str, ...],
    timeout_s: float,
    *,
    label: str = "url-substr",
    poll_s: float = 2.0,
    heartbeat_s: float = 5.0,
) -> bool:
    """Verbose URL-substring waiter.

    Polls every ``poll_s`` seconds, prints a heartbeat at most every
    ``heartbeat_s`` seconds with the current URL + which substrings it
    is hunting for. On timeout, prints a diagnostic block listing the
    most likely operator-actionable causes.
    """
    target_repr = ", ".join(repr(s) for s in substrings)
    vlog(
        f"url-wait BEGIN label={label} looking_for=[{target_repr}] "
        f"timeout={timeout_s}s start_url={_safe_url(page)}"
    )

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    last_heartbeat = 0.0
    last_url_logged: str | None = None
    iteration = 0
    while loop.time() < deadline:
        iteration += 1
        now = loop.time()
        url = _safe_url(page)
        lc = url.lower()
        for needle in substrings:
            if needle.lower() in lc:
                elapsed = now - (deadline - timeout_s)
                vlog(
                    f"url-wait HIT   label={label} matched={needle!r} "
                    f"after={elapsed:.1f}s url={url}"
                )
                return True
        if (now - last_heartbeat) >= heartbeat_s or url != last_url_logged:
            remaining = max(0.0, deadline - now)
            vlog(
                f"url-wait WAIT  label={label} iter={iteration} "
                f"remaining={remaining:.0f}s current_url={url} "
                f"still_looking_for=[{target_repr}]"
            )
            last_heartbeat = now
            last_url_logged = url
        await asyncio.sleep(poll_s)

    final_url = _safe_url(page)
    vlog(f"url-wait FAIL  label={label} final_url={final_url}")
    vlog(
        f"url-wait DIAG  Script is waiting for URL containing [{target_repr}]. "
        f"Current URL: {final_url}. Possible reasons: "
        "(a) you need to click Add-to-Cart manually, "
        "(b) you need to solve a captcha that just appeared, "
        "(c) the SG cart URL pattern changed."
    )
    return False


async def _capture_dom(page, dest: Path, *, screenshot: Path | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vlog(f"capture-dom BEGIN dest={dest} url={_safe_url(page)}")
    html = await page.content()
    # Intentional sync file I/O in async fn: this is a hand-run recon script,
    # not production. The disk write is small + the script blocks on user
    # input between every capture anyway. Same applies to the stat() calls
    # below — they're metadata reads for logging only.
    dest.write_text(html, encoding="utf-8")  # noqa: ASYNC240
    try:
        _dest_mtime = dest.stat().st_mtime  # noqa: ASYNC240
        mtime = datetime.fromtimestamp(_dest_mtime).strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        mtime = "<unknown>"
    rel = dest.relative_to(REPO_ROOT)
    byte_count = len(html.encode("utf-8"))
    vlog(
        f"capture-dom WROTE path={rel} bytes={byte_count} "
        f"mtime={mtime} source_url={_safe_url(page)}"
    )
    _WRITTEN.append((dest, byte_count))
    if screenshot is not None:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(screenshot), full_page=True)
            try:
                _shot_stat = screenshot.stat()  # noqa: ASYNC240
                shot_bytes = _shot_stat.st_size
                shot_mtime = datetime.fromtimestamp(_shot_stat.st_mtime).strftime(
                    "%H:%M:%S",
                )
            except Exception:  # noqa: BLE001
                shot_bytes = -1
                shot_mtime = "<unknown>"
            vlog(
                f"capture-png WROTE path={screenshot.relative_to(REPO_ROOT)} "
                f"bytes={shot_bytes} mtime={shot_mtime}"
            )
            _WRITTEN.append((screenshot, shot_bytes))
        except Exception as exc:  # noqa: BLE001
            vlog(f"capture-png FAIL  path={screenshot} reason={exc!r}")
            _SKIPPED.append((str(screenshot), f"screenshot failed: {exc!r}"))


async def _click_and_observe(
    page,
    locator,
    *,
    name: str,
    settle_s: float = 2.0,
) -> tuple[str, str]:
    """Click a locator, log pre/post URL, return (pre_url, post_url)."""
    pre = _safe_url(page)
    vlog(f"click BEGIN name={name} pre_url={pre}")
    try:
        await locator.click()
    except Exception as exc:  # noqa: BLE001
        vlog(f"click FAIL  name={name} pre_url={pre} reason={exc!r}")
        raise
    vlog(f"click OK    name={name} sleeping {settle_s}s to observe navigation")
    await asyncio.sleep(settle_s)
    post = _safe_url(page)
    changed = "yes" if post != pre else "no"
    vlog(f"click POST  name={name} post_url={post} url_changed={changed}")
    return pre, post


async def _maybe_login(page, email: str, password: str) -> None:
    """If we land on auth.ticketmaster.com, fill creds and wait for OAuth bounce."""
    current = _safe_url(page)
    vlog(f"maybe-login CHECK url={current}")
    if "auth.ticketmaster.com" not in current:
        vlog("maybe-login SKIP  not on auth.ticketmaster.com")
        return
    vlog("maybe-login HIT   on auth.ticketmaster.com — filling email")
    try:
        email_input = sg_selectors.locator(page, "login_email_input")
        if not await _safe_wait_for(
            email_input,
            name="login_email_input",
            state="visible",
            timeout_ms=20_000,
            page=page,
        ):
            vlog("maybe-login WARN  email input never visible; aborting fill")
            return
        await email_input.fill(email)
        vlog("maybe-login filled email, clicking continue")
        await sg_selectors.locator(page, "login_continue_button").click()
        vlog("maybe-login clicked continue, waiting for password step")
        # Password input is rendered as a different step by the SG OAuth
        # provider (PingFederate); it is not part of the SG selector
        # registry because the OAuth host (auth.ticketmaster.com) is
        # outside the SG vendor's DOM scope. A literal locator here is
        # the documented exception.
        pw_input = page.locator("input[type='password']")
        if not await _safe_wait_for(
            pw_input,
            name="oauth_password_input",
            state="visible",
            timeout_ms=30_000,
            page=page,
        ):
            vlog("maybe-login WARN  password input never visible; aborting fill")
            return
        await pw_input.fill(password)
        vlog("maybe-login entered password — pressing Enter (user: solve captcha if shown)")
        await pw_input.press("Enter")
    except Exception as exc:  # noqa: BLE001
        vlog(f"maybe-login WARN  fill error: {exc!r}")

    vlog(
        "maybe-login WAIT  up to 10 minutes for OAuth to bounce back to ticketmaster.sg "
        "(human: solve captchas if prompted)"
    )
    landed = await _wait_until(
        page,
        lambda p: (
            "ticketmaster.sg" in (p.url or "").lower()
            and "identity.ticketmaster.sg" not in (p.url or "").lower()
            and "auth.ticketmaster.com" not in (p.url or "").lower()
        ),
        timeout_seconds=600.0,
        poll=2.0,
    )
    if not landed:
        vlog(f"maybe-login FAIL  login did not complete; aborting final_url={_safe_url(page)}")
        sys.exit(3)
    vlog(f"maybe-login OK    login complete; landed on {_safe_url(page)}")


async def _await_captcha_clear(page, timeout_seconds: float = 600.0) -> None:
    current = _safe_url(page)
    vlog(f"captcha-gate CHECK url={current}")
    if "/ticket/check-captcha/" not in current.lower():
        vlog("captcha-gate SKIP  not on /ticket/check-captcha/")
        return
    vlog(
        "captcha-gate HIT   on /ticket/check-captcha — "
        "USER: solve the Yii image captcha + tick terms + Submit"
    )
    ok = await _wait_until(
        page,
        lambda p: "/ticket/check-captcha/" not in (p.url or "").lower(),
        timeout_seconds=timeout_seconds,
        poll=2.0,
    )
    if not ok:
        vlog("captcha-gate FAIL  captcha was not cleared in time; aborting")
        sys.exit(5)
    vlog(f"captcha-gate OK    captcha cleared; now on {_safe_url(page)}")


async def main() -> None:  # noqa: C901, PLR0912, PLR0915
    vlog("=" * 60)
    vlog("BEGIN CAPTURE RUN")
    vlog(f"repo_root={REPO_ROOT}")
    vlog(f"fixtures_dir={FIXTURES_DIR}")
    vlog(f"sessions_dir={SESSIONS_DIR}")
    vlog(f"screenshots_dir={SCREENSHOTS_DIR}")
    vlog("=" * 60)

    load_dotenv(REPO_ROOT / ".env")
    email = os.environ.get("TM_EMAIL")
    password = os.environ.get("TM_PASSWORD")
    if not email or not password:
        vlog("env-check FAIL TM_EMAIL / TM_PASSWORD not set in .env — aborting")
        sys.exit(2)
    vlog(f"env-check OK   TM_EMAIL set (len={len(email)}); TM_PASSWORD set (len={len(password)})")

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    vlog("dirs ensured")

    ctx = None
    page = None
    final_url = "<unset>"
    browser_open_at_exit = False

    try:
        async with async_playwright() as pw:
            vlog("playwright started; launching persistent Chromium (headless=False)")
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(SESSIONS_DIR),
                headless=False,
                locale="en-SG",
                timezone_id="Asia/Singapore",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={"Accept-Language": "en-SG,en;q=0.8"},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            vlog(f"chromium launched; using page url={_safe_url(page)}")

            # 1. Force login via the SG /login entry point. The SG site 302s
            #    through identity.ticketmaster.sg/sign-in → auth.ticketmaster.com
            #    with all the server-side tokens (placementId, integratorId,
            #    intSiteToken, TMUO, deviceId, disableAutoOptIn) that PingFederate
            #    needs. Navigating to a hand-built OAuth URL drops those tokens
            #    and lands on PingFederate's "Modern Accounts Error Page".
            login_url = "https://ticketmaster.sg/login"
            vlog(f"STEP 1 forcing login navigation: {login_url}")
            try:
                await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
                vlog(f"STEP 1 goto OK url={_safe_url(page)}")
            except Exception as exc:  # noqa: BLE001
                vlog(f"STEP 1 login navigation warning: {exc!r}")

            # If we already have cookies, the OAuth will silently bounce us back
            # to ticketmaster.sg. If not, the form will be visible.
            vlog("STEP 1 waiting for networkidle (up to 15s)")
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
                vlog(f"STEP 1 networkidle reached url={_safe_url(page)}")
            except Exception as exc:  # noqa: BLE001
                vlog(f"STEP 1 networkidle skipped: {exc!r}")
            await _maybe_login(page, email, password)

            # 2. Event detail → schedule row → ticket-area
            vlog(f"STEP 2 opening event url={EVENT_URL}")
            await page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=60_000)
            vlog(f"STEP 2 goto OK url={_safe_url(page)}")
            vlog("STEP 2 waiting for networkidle (up to 15s)")
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
                vlog(f"STEP 2 networkidle reached url={_safe_url(page)}")
            except Exception as exc:  # noqa: BLE001
                vlog(f"STEP 2 networkidle skipped: {exc!r}")
            await _capture_dom(page, SCREENSHOTS_DIR / "01_event_detail.html")

            # Pick the first available "Find tickets" link.
            vlog("STEP 2 hunting Find-tickets link")
            find_link = sg_selectors.locator(page, "event_find_tickets_link")
            if await _safe_wait_for(
                find_link,
                name="event_find_tickets_link",
                state="visible",
                timeout_ms=30_000,
                page=page,
            ):
                try:
                    href = await find_link.get_attribute("href")
                except Exception as exc:  # noqa: BLE001
                    href = f"<href-error: {exc!r}>"
                vlog(f"STEP 2 clicking first Find-tickets link href={href}")
                try:
                    await _click_and_observe(
                        page, find_link, name="event_find_tickets_link", settle_s=2.0
                    )
                except Exception as exc:  # noqa: BLE001
                    vlog(f"STEP 2 click failed: {exc!r}; capturing halt fixture")
                    await _capture_dom(
                        page,
                        SCREENSHOTS_DIR / "halt_event_detail.html",
                        screenshot=SCREENSHOTS_DIR / "halt_event_detail.png",
                    )
                    _SKIPPED.append(
                        ("cart.html", "Find-tickets click failed before ticket area"),
                    )
                    _SKIPPED.append(
                        ("checkout.html", "Find-tickets click failed before ticket area"),
                    )
                    browser_open_at_exit = True
                    await ctx.close()
                    sys.exit(4)
            else:
                vlog("STEP 2 Find-tickets selector never visible; capturing halt fixture")
                await _capture_dom(
                    page,
                    SCREENSHOTS_DIR / "halt_event_detail.html",
                    screenshot=SCREENSHOTS_DIR / "halt_event_detail.png",
                )
                _SKIPPED.append(
                    ("cart.html", "Find-tickets selector never visible"),
                )
                _SKIPPED.append(
                    ("checkout.html", "Find-tickets selector never visible"),
                )
                browser_open_at_exit = True
                await ctx.close()
                sys.exit(4)

            vlog("STEP 2 waiting for networkidle after Find-tickets click (up to 20s)")
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
                vlog(f"STEP 2 networkidle reached url={_safe_url(page)}")
            except Exception as exc:  # noqa: BLE001
                vlog(f"STEP 2 networkidle skipped: {exc!r}")
            await _capture_dom(
                page,
                SCREENSHOTS_DIR / "02_ticket_area.html",
                screenshot=SCREENSHOTS_DIR / "02_ticket_area.png",
            )

            # 3. Choose quantity=2, hit Best Available — retry a few times if needed.
            #    Selectors are sourced from the SG registry so the script stays
            #    in lockstep with config/selectors/ticketmaster_sg.yaml (the live
            #    DOM renders the quantity <select> as
            #    ``TicketForm[ticketPrice][<rowId>]`` inside the AJAX-injected
            #    ``#priceList`` block — NOT ``TicketForm_count`` as the pre-2026
            #    F7.1 recon mistakenly claimed).
            vlog("STEP 3 selecting quantity=2")
            qty_ok = False
            for attempt in range(1, 4):
                vlog(f"STEP 3 quantity-select attempt {attempt}/3 url={_safe_url(page)}")
                try:
                    qty = sg_selectors.locator(page, "ticket_area_quantity_select")
                    if not await _safe_wait_for(
                        qty,
                        name="ticket_area_quantity_select",
                        state="visible",
                        timeout_ms=15_000,
                        page=page,
                    ):
                        raise RuntimeError("quantity select not visible")
                    await qty.select_option(value="2")
                    vlog(f"STEP 3 selected quantity=2 (attempt {attempt})")
                    qty_ok = True
                    break
                except Exception as exc:  # noqa: BLE001
                    vlog(f"STEP 3 qty select attempt {attempt} failed: {exc!r}; reloading")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=30_000)
                        vlog(f"STEP 3 reload OK url={_safe_url(page)}")
                    except Exception as exc2:  # noqa: BLE001
                        vlog(f"STEP 3 reload failed: {exc2!r}")
            if not qty_ok:
                vlog("STEP 3 quantity select never succeeded after 3 attempts")
            try:
                vlog("STEP 3 clicking Best Available")
                best = sg_selectors.locator(page, "ticket_area_best_available_button")
                await _click_and_observe(
                    page, best, name="ticket_area_best_available_button", settle_s=2.0
                )
            except Exception as exc:  # noqa: BLE001
                vlog(f"STEP 3 Best Available click failed: {exc!r}")
                await _capture_dom(page, SCREENSHOTS_DIR / "halt_no_autoMode.html")
                _SKIPPED.append(("cart.html", "Best Available click failed"))
                _SKIPPED.append(("checkout.html", "Best Available click failed"))
                browser_open_at_exit = True
                await ctx.close()
                sys.exit(6)

            # 4. Captcha gate?
            vlog("STEP 4 quick check for captcha-gate URL substring (60s)")
            await _wait_for_url_substring(
                page,
                ("/ticket/check-captcha/",),
                timeout_s=60.0,
                label="captcha-gate",
            )
            await _await_captcha_clear(page)

            # 5. If we get bounced through OAuth at this stage, complete that flow.
            vlog("STEP 5 re-checking for OAuth redirect post-captcha")
            await _maybe_login(page, email, password)

            # 6. Wait for cart URL. SG renders the cart+checkout combined
            #    page at ``/ticket/checkout`` (no trailing slash). The
            #    interstitial ``/ticket/order`` URL appears AFTER the
            #    captcha clears and BEFORE the final ``/ticket/checkout``
            #    landing — we must NOT dump cart.html from that
            #    intermediate page, so we look strictly for
            #    ``/ticket/checkout``. ``_wait_for_url_substring`` does a
            #    plain ``in`` check, so the no-trailing-slash substring
            #    matches both ``/ticket/checkout`` and
            #    ``/ticket/checkout/...``.
            vlog("STEP 6 waiting for /ticket/checkout URL substring (up to 240s)")
            cart_seen = await _wait_for_url_substring(
                page,
                ("/ticket/checkout",),
                timeout_s=240.0,
                label="checkout-landing",
                heartbeat_s=5.0,
            )
            if not cart_seen:
                vlog(
                    "STEP 6 did not reach /ticket/checkout within 4 minutes; "
                    f"capturing current page (url={_safe_url(page)})"
                )
            else:
                vlog(f"STEP 6 reached /ticket/checkout url={_safe_url(page)}")
            vlog("STEP 6 waiting for networkidle (up to 20s)")
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
                vlog(f"STEP 6 networkidle reached url={_safe_url(page)}")
            except Exception as exc:  # noqa: BLE001
                vlog(f"STEP 6 networkidle skipped: {exc!r}")

            await _capture_dom(
                page,
                FIXTURES_DIR / "cart.html",
                screenshot=SCREENSHOTS_DIR / "cart.png",
            )

            # 7. SG combines cart-review + checkout (Contact Details,
            #    Payment Method, Delivery Method, order timer) onto a
            #    single ``/ticket/checkout`` page. There is no "Continue
            #    to checkout" intermediate to click; the only button
            #    that advances from here is "Confirm/Place Order",
            #    which IS the final purchase action and must NEVER be
            #    auto-clicked by this recon script. We therefore dump
            #    the same DOM to ``checkout.html`` so downstream F7.4
            #    fixtures see both filenames, but we do not navigate.
            vlog("STEP 7 SG cart+checkout share one page — re-dumping DOM to checkout.html")
            await _capture_dom(
                page,
                FIXTURES_DIR / "checkout.html",
                screenshot=SCREENSHOTS_DIR / "checkout.png",
            )

            vlog("done; closing browser (no purchase performed)")
            final_url = _safe_url(page)
            await ctx.close()
            ctx = None
            browser_open_at_exit = False
    finally:
        if ctx is not None and page is not None:
            try:
                final_url = _safe_url(page)
            except Exception:  # noqa: BLE001
                pass
            browser_open_at_exit = True

        elapsed = time.monotonic() - _RUN_STARTED_AT
        written_paths = [str(p.relative_to(REPO_ROOT)) for p, _ in _WRITTEN] or ["<none>"]
        # Cross-check which target fixtures were actually written this run.
        target_fixtures = {
            "cart.html": FIXTURES_DIR / "cart.html",
            "checkout.html": FIXTURES_DIR / "checkout.html",
        }
        written_set = {p.resolve() for p, _ in _WRITTEN}
        for label, dest in target_fixtures.items():
            if dest.resolve() not in written_set and not any(
                reason for tag, reason in _SKIPPED if tag == label
            ):
                _SKIPPED.append((label, "not reached this run"))

        skipped_repr = (
            ", ".join(f"{tag} ({reason})" for tag, reason in _SKIPPED) or "<none>"
        )

        vlog("=== END OF CAPTURE ===")
        vlog(f"Wrote: {written_paths}")
        vlog(f"Skipped: {skipped_repr}")
        vlog(f"Total runtime: {elapsed:.1f}s")
        vlog(f"Final URL: {final_url}")
        vlog(f"Browser still open: {'Y' if browser_open_at_exit else 'N'}")
        vlog("You can close the browser window now." if browser_open_at_exit
             else "Browser already closed by script.")
        vlog("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
