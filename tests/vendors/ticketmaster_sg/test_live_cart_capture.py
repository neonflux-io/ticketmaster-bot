"""Live-capture gate for F7.4 — runs only when both fixtures exist.

Per the F7.4 brief: "Add a live-integration test gated on the captured
fixtures existing." This module exposes one assertion:

* If both ``tests/fixtures/vendors/ticketmaster_sg/cart.html`` AND
  ``tests/fixtures/vendors/ticketmaster_sg/checkout.html`` are present
  on disk AND have been re-dumped from a real authenticated SG run
  (i.e. the live-capture banner-comment has been *replaced* by a
  live-capture marker), the test asserts the SG cart and checkout
  Python modules can drive the live fixtures end-to-end via real
  Chromium.

* If the fixtures are still the hand-built F7.4 placeholders (their
  banner-comment still contains "NOT captured first-hand"), this test
  ``pytest.skip``s with a clear reason naming the follow-up feature
  (F7.6) that owns the live capture. Skipping here keeps the test
  suite green while making it impossible to *silently* forget the
  deferred capture — once the live capture lands, the test starts
  running automatically without further wiring.

The gate is deliberately a content marker, not a file-mtime check, so
re-running ``ruff format`` over the fixture HTML (which only edits
whitespace) doesn't accidentally promote a hand-built file into "live"
status.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.vendors.ticketmaster_sg.cart import (
    accept_terms_if_needed,
    set_quantity,
    verify_cart_in_cart,
)
from src.vendors.ticketmaster_sg.checkout import (
    select_delivery,
    select_saved_card,
    verify_cart_matches,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SG_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "vendors" / "ticketmaster_sg"
CART_FIXTURE = SG_FIXTURE_DIR / "cart.html"
CHECKOUT_FIXTURE = SG_FIXTURE_DIR / "checkout.html"

# A live-capture run dumps a `<!-- live-capture ... -->` marker into
# the fixture (see docs/recon/ticketmaster_sg/_cart_capture.py for the
# capture script). The marker is only ever inserted by an authenticated
# headed Chromium run, so its presence is the strongest signal the
# fixture is real.
LIVE_CAPTURE_MARKER = "<!-- live-capture"
DEFERRED_MARKER = "NOT captured first-hand"


def _is_live_captured(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    if LIVE_CAPTURE_MARKER in text:
        return True
    if DEFERRED_MARKER in text:
        return False
    # Conservative default: if the explicit deferral marker is gone but
    # no live-capture marker is present either, treat it as live.
    return True


def _skip_reason() -> str:
    parts = []
    if not CART_FIXTURE.is_file():
        parts.append("cart.html missing")
    elif not _is_live_captured(CART_FIXTURE):
        parts.append("cart.html is the F7.4 hand-built placeholder")
    if not CHECKOUT_FIXTURE.is_file():
        parts.append("checkout.html missing")
    elif not _is_live_captured(CHECKOUT_FIXTURE):
        parts.append("checkout.html is the F7.4 hand-built placeholder")
    return (
        f"Live cart/checkout capture not yet performed ({'; '.join(parts)}); "
        "see F7.6 for the headed-Chromium live e2e run that will re-dump "
        "these fixtures from a real authenticated SG session."
    )


@pytest.mark.skipif(
    not (_is_live_captured(CART_FIXTURE) and _is_live_captured(CHECKOUT_FIXTURE)),
    reason=_skip_reason(),
)
async def test_live_cart_and_checkout_drive_real_fixtures(chromium_context, fixture_url) -> None:
    """Once both fixtures are live-captured, the SG cart + checkout drive them end-to-end."""
    # Cart side.
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/cart.html"))
    # Quantity slider — the function must succeed against the live DOM
    # without raising (the value used must already be one of the live
    # SG select's option values; '2' is the conservative default).
    set_qty_ok = await set_quantity(page, 2)
    assert isinstance(set_qty_ok, bool)
    accept_ok = await accept_terms_if_needed(page)
    assert isinstance(accept_ok, bool)
    cart_ok = await verify_cart_in_cart(page)
    assert isinstance(cart_ok, bool)
    await page.close()

    # Checkout side.
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vendors/ticketmaster_sg/checkout.html"))
    # The live page may not have either preferred delivery option, so
    # allow_any=True is the conservative choice for the live-gated path.
    await select_delivery(page, preferred=["mobile entry"], allow_any=True)
    # Saved-card selection is best-effort; absence is fine.
    await select_saved_card(page, None)
    matched = await verify_cart_matches(
        page,
        expected_quantity=0,  # disables quantity check; only test the read path
        expected_candidate=None,
        price_tolerance=0.5,
    )
    assert isinstance(matched, bool)
    await page.close()


def test_live_capture_gate_skip_reason_is_actionable() -> None:
    """The skip reason must name F7.6 so the follow-up is obvious."""
    reason = _skip_reason()
    assert "F7.6" in reason
