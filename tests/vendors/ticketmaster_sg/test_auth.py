"""Real-Chromium tests for :mod:`src.vendors.ticketmaster_sg.auth`.

Drives the distilled fixtures captured during F7.1 recon to exercise
captcha detection, cookie-based login probing, and the OAuth URL
builder. The complementary live-integration test lives in
``test_live_auth.py`` and gates on network reachability.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.vendors.ticketmaster_sg import auth as sg_auth

REPO_ROOT = Path(__file__).resolve().parents[3]
SG_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"


# ---------------------------------------------------------------------------
# Pure-Python: OAuth URL builder + constants
# ---------------------------------------------------------------------------


def test_oauth_url_carries_sg_client_and_redirect() -> None:
    url = sg_auth.build_sg_oauth_url()
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.ticketmaster.com"
    assert parsed.path == "/as/authorization.oauth2"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["1a554b2c04dc.web.ticketmaster.sg"]
    assert qs["redirect_uri"] == ["https://identity.ticketmaster.sg/exchange"]
    assert qs["visualPresets"] == ["tmsg"]
    assert qs["response_type"] == ["code"]


def test_required_cookie_set_excludes_bid() -> None:
    """``BID`` alone must not satisfy the cookie probe (recon doc).

    The recon doc records that ``BID`` is set on the first anonymous
    visit, so a logged-in probe that included it would false-positive
    on anonymous sessions.
    """
    assert "BID" not in sg_auth.SG_AUTH_REQUIRED_COOKIES
    assert sg_auth.SG_AUTH_REQUIRED_COOKIES == {"eps_sid", "tmpt", "TIXPUISID"}


# ---------------------------------------------------------------------------
# Real Chromium: captcha detection on the captured fixture
# ---------------------------------------------------------------------------


async def test_has_captcha_detects_yii_image_on_check_captcha_fixture(
    chromium_context,
    fixture_url,
) -> None:
    """The distilled check-captcha fixture has both Yii image input + reCAPTCHA iframe."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/check_captcha.html"))
    assert await sg_auth.has_captcha(page) is True
    # And specifically the Yii probe alone is true (image + input present).
    assert await sg_auth._yii_captcha_visible(page) is True


async def test_has_captcha_false_on_event_detail_fixture(
    chromium_context,
    fixture_url,
) -> None:
    """Event-detail page has neither captcha; ``has_captcha`` must return False."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    assert await sg_auth.has_captcha(page) is False


async def test_has_captcha_detects_invisible_recaptcha_on_login_fixture(
    chromium_context,
    fixture_url,
) -> None:
    """Login fixture mounts only the invisible reCAPTCHA iframe — must still detect."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/login.html"))
    # The Yii image probe is False here (no <img#TicketForm_verifyCode-image>),
    # but the overall ``has_captcha`` must still report True because the
    # reCAPTCHA iframe is mounted.
    assert await sg_auth._yii_captcha_visible(page) is False
    assert await sg_auth.has_captcha(page) is True


# ---------------------------------------------------------------------------
# Real Chromium: is_logged_in cookie probe
# ---------------------------------------------------------------------------


async def test_is_logged_in_false_without_cookies(chromium, monkeypatch) -> None:
    """Fresh context (no SG cookies) probed against the local fixture is logged-out.

    We monkeypatch ``SG_HOME_URL`` to point at a local file:// fixture
    so the probe stays offline and deterministic.
    """
    home_url = (SG_FIXTURE_DIR / "event_detail.html").as_uri()
    monkeypatch.setattr(sg_auth, "SG_HOME_URL", home_url)

    context = await chromium.new_context()
    try:
        assert await sg_auth.is_logged_in(context) is False
    finally:
        await context.close()


async def test_is_logged_in_false_when_only_bid_cookie_present(chromium, monkeypatch) -> None:
    """Anonymous visitor with only ``BID`` set must still be logged-out."""
    home_url = (SG_FIXTURE_DIR / "event_detail.html").as_uri()
    monkeypatch.setattr(sg_auth, "SG_HOME_URL", home_url)

    context = await chromium.new_context()
    try:
        # Visit the fixture under a real http origin so cookies stick.
        # file:// cookies are silently dropped by Chromium, so use
        # ticketmaster.sg as the cookie origin (no real network goes out
        # because we monkeypatched SG_HOME_URL to the file:// fixture
        # — the cookies are only ever read back via context.cookies()).
        await context.add_cookies(
            [
                {
                    "name": "BID",
                    "value": "anon-only",
                    "domain": ".ticketmaster.sg",
                    "path": "/",
                }
            ]
        )
        assert await sg_auth.is_logged_in(context) is False
    finally:
        await context.close()


async def test_is_logged_in_true_with_full_sg_cookie_set(chromium, monkeypatch) -> None:
    """All four required SG cookies present → ``is_logged_in`` returns True."""
    home_url = (SG_FIXTURE_DIR / "event_detail.html").as_uri()
    monkeypatch.setattr(sg_auth, "SG_HOME_URL", home_url)

    context = await chromium.new_context()
    try:
        cookies = [
            {
                "name": name,
                "value": f"recon-{name.lower()}",
                "domain": ".ticketmaster.sg",
                "path": "/",
            }
            for name in ("BID", "eps_sid", "tmpt", "TIXPUISID")
        ]
        await context.add_cookies(cookies)
        assert await sg_auth.is_logged_in(context) is True
    finally:
        await context.close()


# ---------------------------------------------------------------------------
# Captcha-wait helper finishes immediately when no captcha
# ---------------------------------------------------------------------------


async def test_wait_for_human_if_captcha_returns_true_when_no_captcha(
    chromium_context, fixture_url
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/event_detail.html"))
    # timeout=0.5 so the helper returns immediately for the no-captcha case.
    result = await sg_auth.wait_for_human_if_captcha(page, timeout_seconds=0.5)
    assert result is True


async def test_wait_for_human_if_captcha_times_out_when_unsolved(
    chromium_context, fixture_url
) -> None:
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/check_captcha.html"))
    # The fixture leaves the Yii image and the reCAPTCHA iframe in
    # place, so the helper must eventually time out and return False.
    result = await sg_auth.wait_for_human_if_captcha(page, timeout_seconds=1.0)
    assert result is False


# ---------------------------------------------------------------------------
# Sanity: AuthError import path is stable
# ---------------------------------------------------------------------------


def test_auth_error_is_exception_subclass() -> None:
    assert issubclass(sg_auth.AuthError, Exception)


def test_module_exports_required_names() -> None:
    required = {
        "AuthError",
        "SG_AUTH_REQUIRED_COOKIES",
        "SG_HOME_URL",
        "SG_OAUTH_AUTHORIZE_URL",
        "SG_OAUTH_CLIENT_ID",
        "SG_OAUTH_REDIRECT_URI",
        "SG_OAUTH_VISUAL_PRESETS",
        "build_sg_oauth_url",
        "has_captcha",
        "is_logged_in",
        "login",
        "wait_for_human_if_captcha",
    }
    missing = required - set(sg_auth.__all__)
    assert not missing, f"sg_auth module is missing exports: {missing}"
