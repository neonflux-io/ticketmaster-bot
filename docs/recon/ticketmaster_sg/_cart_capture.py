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
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"
SESSIONS_DIR = REPO_ROOT / "sessions" / "sg-capture"
SCREENSHOTS_DIR = REPO_ROOT / "docs" / "recon" / "ticketmaster_sg" / "f7_4_capture"

EVENT_URL = "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
HOME_URL = "https://ticketmaster.sg/"
PROFILE_URL = "https://ticketmaster.sg/profile"


def _print(msg: str) -> None:
    sys.stdout.write(f"[capture] {msg}\n")
    sys.stdout.flush()


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


async def _wait_for_url_substring(page, substrings: tuple[str, ...], timeout_s: float) -> bool:
    return await _wait_until(
        page,
        lambda p: any(s.lower() in (p.url or "").lower() for s in substrings),
        timeout_s,
    )


async def _capture_dom(page, dest: Path, *, screenshot: Path | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    html = await page.content()
    dest.write_text(html, encoding="utf-8")
    _print(f"saved {dest.relative_to(REPO_ROOT)} ({len(html)} bytes; url={page.url})")
    if screenshot is not None:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(screenshot), full_page=True)
            _print(f"saved {screenshot.relative_to(REPO_ROOT)}")
        except Exception as exc:  # noqa: BLE001
            _print(f"(screenshot failed: {exc})")


async def _maybe_login(page, email: str, password: str) -> None:
    """If we land on auth.ticketmaster.com, fill creds and wait for OAuth bounce."""
    if "auth.ticketmaster.com" not in (page.url or ""):
        return
    _print(f"login page detected (url={page.url}); filling email")
    try:
        await page.locator("input#email-input").wait_for(state="visible", timeout=20_000)
        await page.locator("input#email-input").fill(email)
        await page.locator("button[name='sign-in']").click()
        _print("entered email, waiting for password step")
        await page.locator("input[type='password']").wait_for(state="visible", timeout=30_000)
        await page.locator("input[type='password']").fill(password)
        _print("entered password — pressing Enter (user: solve any captcha if shown)")
        await page.locator("input[type='password']").press("Enter")
    except Exception as exc:  # noqa: BLE001
        _print(f"login fill warning: {exc}")

    _print(
        "waiting up to 10 minutes for OAuth to bounce back to ticketmaster.sg "
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
        _print(f"login did not complete; aborting (final url={page.url})")
        sys.exit(3)
    _print(f"login complete; landed on {page.url}")


async def _await_captcha_clear(page, timeout_seconds: float = 600.0) -> None:
    if "/ticket/check-captcha/" not in (page.url or "").lower():
        return
    _print("on /ticket/check-captcha — USER: solve the Yii image captcha + tick terms + Submit")
    ok = await _wait_until(
        page,
        lambda p: "/ticket/check-captcha/" not in (p.url or "").lower(),
        timeout_seconds=timeout_seconds,
        poll=2.0,
    )
    if not ok:
        _print("captcha was not cleared in time; aborting")
        sys.exit(5)
    _print(f"captcha cleared; now on {page.url}")


async def main() -> None:  # noqa: C901, PLR0912, PLR0915
    load_dotenv(REPO_ROOT / ".env")
    email = os.environ.get("TM_EMAIL")
    password = os.environ.get("TM_PASSWORD")
    if not email or not password:
        _print("TM_EMAIL / TM_PASSWORD not set in .env — aborting")
        sys.exit(2)

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SESSIONS_DIR),
            headless=False,
            locale="en-SG",
            timezone_id="Asia/Singapore",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.8"},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # 1. Force login via the OAuth URL (avoids the /profile drop that just
        #    silently shows the visitor page when not logged in).
        from urllib.parse import urlencode

        oauth_url = "https://auth.ticketmaster.com/as/authorization.oauth2?" + urlencode(
            {
                "client_id": "1a554b2c04dc.web.ticketmaster.sg",
                "response_type": "code",
                "scope": "openid profile phone email tm",
                "redirect_uri": "https://identity.ticketmaster.sg/exchange",
                "visualPresets": "tmsg",
                "lang": "en-sg",
            }
        )
        _print(f"forcing login: {oauth_url}")
        try:
            await page.goto(oauth_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            _print(f"OAuth navigation warning: {exc}")

        # If we already have cookies, the OAuth will silently bounce us back
        # to ticketmaster.sg. If not, the form will be visible.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        await _maybe_login(page, email, password)

        # 2. Event detail → schedule row → ticket-area
        _print(f"opening event: {EVENT_URL}")
        await page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        await _capture_dom(page, SCREENSHOTS_DIR / "01_event_detail.html")

        # Pick the first available "Find tickets" link.
        find_link = page.locator(
            "table.auto-game-list tbody tr[data-key] a.btn-primary, "
            "tr[data-key] a[href*='/ticket/area/']"
        ).first
        try:
            await find_link.wait_for(state="visible", timeout=30_000)
            href = await find_link.get_attribute("href")
            _print(f"clicking first Find-tickets link (href={href})")
            await find_link.click()
        except Exception as exc:  # noqa: BLE001
            _print(f"could not click Find-tickets; capturing event detail (err={exc})")
            await _capture_dom(
                page,
                SCREENSHOTS_DIR / "halt_event_detail.html",
                screenshot=SCREENSHOTS_DIR / "halt_event_detail.png",
            )
            await ctx.close()
            sys.exit(4)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        await _capture_dom(
            page,
            SCREENSHOTS_DIR / "02_ticket_area.html",
            screenshot=SCREENSHOTS_DIR / "02_ticket_area.png",
        )

        # 3. Choose quantity=2, hit Best Available — retry a few times if needed
        for attempt in range(1, 4):
            try:
                qty = page.locator("select#TicketForm_count").first
                await qty.wait_for(state="visible", timeout=15_000)
                await qty.select_option(value="2")
                _print(f"selected quantity=2 (attempt {attempt})")
                break
            except Exception as exc:  # noqa: BLE001
                _print(f"qty select attempt {attempt} failed: {exc}; reloading")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
                except Exception:  # noqa: BLE001
                    pass
        try:
            await page.locator("button#autoMode").first.click()
            _print("clicked Best Available")
        except Exception as exc:  # noqa: BLE001
            _print(f"could not click Best Available: {exc}")
            await _capture_dom(page, SCREENSHOTS_DIR / "halt_no_autoMode.html")
            await ctx.close()
            sys.exit(6)

        # 4. Captcha gate?
        await _wait_for_url_substring(page, ("/ticket/check-captcha/",), timeout_s=60.0)
        await _await_captcha_clear(page)

        # 5. If we get bounced through OAuth at this stage, complete that flow.
        await _maybe_login(page, email, password)

        # 6. Wait for cart URL
        cart_seen = await _wait_for_url_substring(
            page,
            ("/ticket/checkout/", "/cart"),
            timeout_s=240.0,
        )
        if not cart_seen:
            _print(
                "did not reach /ticket/checkout/ within 4 minutes; capturing current page "
                f"(url={page.url})"
            )
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass

        await _capture_dom(
            page,
            FIXTURES_DIR / "cart.html",
            screenshot=SCREENSHOTS_DIR / "cart.png",
        )

        # 7. Advance one more step (delivery/payment) — but DO NOT click any
        #    final Pay / Place Order button.
        next_btn = page.locator(
            "button:has-text('Continue'), button:has-text('Checkout'), "
            "button:has-text('Proceed'), a:has-text('Checkout'), "
            "button:has-text('Next'), button:has-text('Confirm')"
        ).first
        try:
            if await next_btn.is_visible(timeout=2_500):
                _print("clicking first non-purchase 'Continue/Checkout/Proceed/Next' button")
                await next_btn.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:  # noqa: BLE001
                    pass
                if "check-captcha" in (page.url or ""):
                    _print("second captcha gate hit — USER: solve it")
                    await _await_captcha_clear(page)
            else:
                _print("no Continue/Checkout button visible; capturing same page as checkout")
        except Exception as exc:  # noqa: BLE001
            _print(f"continue-to-checkout warning: {exc}")

        await _capture_dom(
            page,
            FIXTURES_DIR / "checkout.html",
            screenshot=SCREENSHOTS_DIR / "checkout.png",
        )

        _print("done; closing browser (no purchase performed)")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
