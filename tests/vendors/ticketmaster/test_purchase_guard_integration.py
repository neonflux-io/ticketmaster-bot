"""Real-Chromium integration tests for the US/CA checkout purchase guard.

These tests use ``page.set_content`` with a minimal DOM fragment so the
focus is purely on the :func:`src.utils.purchase_guard.gate` integration
in :func:`src.vendors.ticketmaster.checkout.run_checkout`.
"""

from __future__ import annotations

import pytest

from src.utils import purchase_guard
from src.vendors.ticketmaster import checkout as tm_checkout
from src.vendors.ticketmaster.checkout import run_checkout

# Minimal HTML fragment exposing exactly the elements the US checkout
# flow reads: a delivery radio, a Place Order button matching the
# ``place_order_button`` selector list, and a click counter the test
# can assert against.
_CHECKOUT_HTML = """
<!DOCTYPE html>
<html>
  <body>
    <div data-bdd="delivery-option">
      <label>Mobile Entry</label>
      <input type="radio" name="delivery" value="mobile">
    </div>
    <button>Place Order</button>
    <script>
      window.__tmPlaceOrderClicks = 0;
      document.querySelector('button').addEventListener(
        'click',
        function () { window.__tmPlaceOrderClicks += 1; },
        true,
      );
    </script>
  </body>
</html>
"""


async def _set_minimal_checkout(page) -> None:  # noqa: ANN001
    await page.set_content(_CHECKOUT_HTML)


async def _patch_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the captcha-wait so the test never blocks on a human."""

    async def _no_captcha(page, *, timeout_seconds: float = 300.0) -> bool:  # noqa: ANN001
        return True

    monkeypatch.setattr(tm_checkout.auth_module, "wait_for_human_if_captcha", _no_captcha)


# ---------------------------------------------------------------------------
# auto_purchase=False — Place Order never even attempted.
# ---------------------------------------------------------------------------


async def test_us_auto_purchase_false_never_clicks(
    chromium_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(purchase_guard.PURCHASE_OVERRIDE_ENV, raising=False)
    page = await chromium_context.new_page()
    await _set_minimal_checkout(page)

    result = await run_checkout(
        page,
        auto_purchase=False,
        card_last_four=None,
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile entry"],
    )

    assert result is True
    clicks = await page.evaluate("() => window.__tmPlaceOrderClicks")
    assert clicks == 0


# ---------------------------------------------------------------------------
# auto_purchase=True + guard NOT overridden → blocked, no click, soft fail.
# ---------------------------------------------------------------------------


async def test_us_auto_purchase_true_blocked_when_env_unset(
    chromium_context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(purchase_guard.PURCHASE_OVERRIDE_ENV, raising=False)
    await _patch_captcha(monkeypatch)

    page = await chromium_context.new_page()
    await _set_minimal_checkout(page)

    with caplog.at_level("ERROR", logger="ticketmaster-bot"):
        result = await run_checkout(
            page,
            auto_purchase=True,
            card_last_four=None,
            action_delay=(0.0, 0.0),
            preferred_delivery=["mobile entry"],
            expected_quantity=0,
            captcha_timeout_seconds=0.1,
        )

    assert result is False
    clicks = await page.evaluate("() => window.__tmPlaceOrderClicks")
    assert clicks == 0
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "BLOCKED" in joined or "blocked by purchase guard" in joined.lower()


async def test_us_auto_purchase_true_blocked_with_wrong_env_value(
    chromium_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(purchase_guard.PURCHASE_OVERRIDE_ENV, "nope")
    await _patch_captcha(monkeypatch)

    page = await chromium_context.new_page()
    await _set_minimal_checkout(page)

    result = await run_checkout(
        page,
        auto_purchase=True,
        card_last_four=None,
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile entry"],
        expected_quantity=0,
        captcha_timeout_seconds=0.1,
    )
    assert result is False
    clicks = await page.evaluate("() => window.__tmPlaceOrderClicks")
    assert clicks == 0


# ---------------------------------------------------------------------------
# auto_purchase=True + guard explicitly overridden → click proceeds.
# ---------------------------------------------------------------------------


async def test_us_auto_purchase_true_clicks_when_env_overridden(
    chromium_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        purchase_guard.PURCHASE_OVERRIDE_ENV,
        purchase_guard.PURCHASE_OVERRIDE_VALUE,
    )
    await _patch_captcha(monkeypatch)

    page = await chromium_context.new_page()
    await _set_minimal_checkout(page)

    await run_checkout(
        page,
        auto_purchase=True,
        card_last_four=None,
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile entry"],
        expected_quantity=0,
        captcha_timeout_seconds=0.1,
    )
    clicks = await page.evaluate("() => window.__tmPlaceOrderClicks")
    assert clicks == 1
