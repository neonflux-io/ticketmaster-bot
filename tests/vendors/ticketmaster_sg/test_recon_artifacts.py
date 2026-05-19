"""Validate the F7.1 ticketmaster.sg recon artefacts.

The F7.1 feature produces three classes of deliverable:

1. **The recon report** (``docs/recon/ticketmaster_sg.md``) and its
   supporting raw captures (HTML snapshots, screenshots, HAR).
2. **Distilled fixtures** (``tests/fixtures/vendors/ticketmaster_sg/*.html``)
   that mirror the live DOM closely enough for the SG vendor adapter
   to drive them in Playwright.
3. **Draft selector registry** (``config/selectors/ticketmaster_sg.yaml``)
   whose every logical name resolves to at least one real element on
   the matching fixture, under real headless Chromium.

This test module verifies (1) all deliverables are present and
non-trivial, and (2) every draft selector the recon claims is actually
matchable by Playwright against the distilled fixture for the page it
came from. This is the externally-checkable evidence that
``sg.recon-completed`` is fulfilled.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "docs" / "recon" / "ticketmaster_sg"
RECON_DOC = REPO_ROOT / "docs" / "recon" / "ticketmaster_sg.md"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"
SELECTORS_YAML = REPO_ROOT / "config" / "selectors" / "ticketmaster_sg.yaml"


# ---------------------------------------------------------------------------
# (1) deliverables-present checks
# ---------------------------------------------------------------------------


def test_recon_doc_exists_and_is_substantial() -> None:
    """``docs/recon/ticketmaster_sg.md`` is committed and >5 KB."""
    assert RECON_DOC.is_file(), f"Recon doc missing: {RECON_DOC}"
    size = RECON_DOC.stat().st_size
    assert size > 5_000, f"Recon doc too small ({size} bytes); needs real recon notes"
    text = RECON_DOC.read_text(encoding="utf-8")
    # Critical facts the doc MUST mention by name; if any are missing the
    # doc is not actually the recon report it claims to be.
    must_mention = [
        "ticketmaster.sg",
        "PGL CS",
        "reCAPTCHA",
        "Yii",
        "Queue-It",
        "TIXPUISID",
        "auth.ticketmaster.com",
        "PayNow",
        "GrabPay",
        "SGD",
        "/ticket/check-captcha/",
        "/ticket/area/",
    ]
    missing = [m for m in must_mention if m not in text]
    assert not missing, f"Recon doc is missing required facts: {missing}"


@pytest.mark.parametrize(
    "stem",
    [
        "01_home",
        "02_event_detail",
        "03_seat_selection",
        "04_captcha",
        "05_login",
        "06_listing",
    ],
)
def test_raw_snapshot_pair_exists(stem: str) -> None:
    """Each recon stage has both an HTML snapshot and a PNG screenshot."""
    html = RECON_DIR / f"{stem}.html"
    png = RECON_DIR / f"{stem}.png"
    assert html.is_file(), f"Missing recon HTML: {html}"
    assert png.is_file(), f"Missing recon PNG: {png}"
    # >5 KB HTML guards against blank pages.
    assert html.stat().st_size > 5_000, f"Recon HTML too small: {html}"
    # >5 KB PNG guards against 1x1 placeholders.
    assert png.stat().st_size > 5_000, f"Recon PNG too small: {png}"


def test_har_file_exists() -> None:
    """The HAR capture is committed and non-trivial."""
    har = RECON_DIR / "ticketmaster_sg.har"
    assert har.is_file(), f"Missing HAR: {har}"
    # >100 KB after stripping binary bodies; the live capture has ~500
    # requests, so even with bodies stripped we expect headers + URLs
    # to weigh several hundred KB.
    assert har.stat().st_size > 100_000, f"HAR too small: {har.stat().st_size} bytes"


def test_har_capture_script_exists_and_runs_python() -> None:
    """The HAR capture script is committed and is a real Python module."""
    script = RECON_DIR / "_har_capture.py"
    assert script.is_file(), f"Missing HAR capture script: {script}"
    code = script.read_text(encoding="utf-8")
    assert "async_playwright" in code
    assert "record_har_path" in code


# ---------------------------------------------------------------------------
# (2) fixtures + selectors coherence under real headless Chromium
# ---------------------------------------------------------------------------


def _load_selectors() -> dict[str, list[str]]:
    raw = yaml.safe_load(SELECTORS_YAML.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "ticketmaster_sg.yaml must be a top-level mapping"
    assert raw, "ticketmaster_sg.yaml must be non-empty"
    for name, fallbacks in raw.items():
        assert isinstance(fallbacks, list) and fallbacks, (
            f"selector {name!r} must be a non-empty list of strings"
        )
        for sel in fallbacks:
            assert isinstance(sel, str) and sel.strip(), (
                f"selector {name!r} contains a non-string or empty fallback: {sel!r}"
            )
    return raw


def test_selectors_yaml_loads_and_has_minimum_entries() -> None:
    """Selector YAML loads cleanly and has the recon-required logical names."""
    selectors = _load_selectors()
    assert len(selectors) >= 20, (
        f"Selector YAML must declare at least 20 logical names (got {len(selectors)})"
    )
    required = {
        "event_date_row",
        "event_find_tickets_link",
        "ticket_area_quantity_select",
        "ticket_area_best_available_button",
        "ticket_type_row",
        "ticket_type_quantity_select",
        "csrf_input",
        "check_captcha_form",
        "captcha_image",
        "captcha_input",
        "captcha_terms_checkbox",
        "captcha_submit_button",
        "recaptcha_response_input",
        "login_email_input",
        "login_continue_button",
        "login_nds_pmd_input",
    }
    missing = required - selectors.keys()
    assert not missing, f"Selector YAML is missing required logical names: {missing}"


# Mapping logical name -> fixture file it should resolve against (and
# whether the resolved Locator must be visible; some captcha inputs
# are CSS-hidden in the fixture).
_FIXTURE_FOR: dict[str, tuple[str, bool]] = {
    # event detail page
    "event_date_row": ("event_detail.html", True),
    "event_find_tickets_link": ("event_detail.html", True),
    "event_date_select": ("event_detail.html", True),
    # ticket area page
    "ticket_area_event_select": ("ticket_area.html", True),
    "ticket_area_quantity_select": ("ticket_area.html", True),
    "ticket_area_best_available_button": ("ticket_area.html", True),
    "ticket_area_map_container": ("ticket_area.html", True),
    # ticket-type fragment (AJAX-injected; we render it as a full page)
    "ticket_type_row": ("ticket_ticket.html", True),
    "ticket_type_price_text": ("ticket_ticket.html", True),
    "ticket_type_name_text": ("ticket_ticket.html", True),
    "ticket_type_quantity_select": ("ticket_ticket.html", True),
    "ticket_type_price_size_input": ("ticket_ticket.html", False),
    "ticket_form_submit_button": ("ticket_ticket.html", True),
    "csrf_input": ("ticket_ticket.html", False),
    # check-captcha page
    "check_captcha_form": ("check_captcha.html", True),
    "captcha_image": ("check_captcha.html", True),
    "captcha_input": ("check_captcha.html", True),
    "captcha_terms_checkbox": ("check_captcha.html", True),
    "captcha_submit_button": ("check_captcha.html", True),
    "recaptcha_response_input": ("check_captcha.html", False),
    "captcha_iframe": ("check_captcha.html", False),
    # login page
    "login_email_input": ("login.html", True),
    "login_continue_button": ("login.html", True),
    "login_nds_pmd_input": ("login.html", False),
}


@pytest.mark.parametrize(
    ("logical_name", "fixture", "must_be_visible"),
    [(name, fix, vis) for name, (fix, vis) in _FIXTURE_FOR.items()],
)
async def test_selector_resolves_on_fixture(
    chromium_context,  # noqa: ANN001 - pytest fixture (Playwright BrowserContext)
    fixture_url,  # noqa: ANN001 - pytest fixture (Callable[[str], str])
    logical_name: str,
    fixture: str,
    must_be_visible: bool,
) -> None:
    """Each draft selector matches ≥1 element on its target fixture.

    This is the recon equivalent of the existing
    ``dom.selector-registry-resolves`` assertion for the US site:
    every logical name we ship in the SG selector YAML must point at a
    real element on the real-shaped fixture we extracted from the live
    DOM. If this test fails for any name, either the fixture is wrong
    or the recon doc misidentified the selector.
    """
    selectors = _load_selectors()
    assert logical_name in selectors, f"selector YAML missing {logical_name!r}"
    combined = ", ".join(selectors[logical_name])

    page = await chromium_context.new_page()
    try:
        await page.goto(fixture_url(f"vendors/ticketmaster_sg/{fixture}"))
        locator = page.locator(combined)
        count = await locator.count()
        assert count >= 1, (
            f"Selector {logical_name!r} ({combined!r}) matched 0 elements "
            f"on tests/fixtures/vendors/ticketmaster_sg/{fixture}"
        )
        if must_be_visible:
            assert await locator.first.is_visible(timeout=2000), (
                f"Selector {logical_name!r} matched but first element is not visible "
                f"on tests/fixtures/vendors/ticketmaster_sg/{fixture}"
            )
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# (3) captcha vendor identification check
# ---------------------------------------------------------------------------


def test_captcha_fixture_carries_yii_and_recaptcha_markers() -> None:
    """The captcha fixture exposes both captcha vendors the recon doc claims.

    The recon doc states the SG site uses Yii image-CAPTCHA AND invisible
    reCAPTCHA Enterprise side-by-side. If we ever drift to a single-vendor
    captcha fixture, this test catches it.
    """
    fixture = FIXTURES_DIR / "check_captcha.html"
    text = fixture.read_text(encoding="utf-8")
    assert "TicketForm_verifyCode" in text, "Yii image-CAPTCHA input must be present"
    assert "TicketForm_verifyCode-image" in text, "Yii image-CAPTCHA <img> must be present"
    assert "g-recaptcha-response" in text, "reCAPTCHA response input must be present"
    assert "reCAPTCHA" in text, "reCAPTCHA iframe title must be present"
    assert "TicketForm_agree" in text, "Terms checkbox (Yii form) must be present"
    assert "_csrf" in text, "Yii CSRF input must be present"
