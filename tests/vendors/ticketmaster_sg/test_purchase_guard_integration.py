"""Real-Chromium integration tests for the SG checkout purchase guard.

These tests use ``page.set_content`` rather than the on-disk SG checkout
fixture so they are decoupled from the larger F7.4 fixture (which has
its own moving parts). The focus is purely on the
:func:`src.utils.purchase_guard.gate` integration in
:func:`src.vendors.ticketmaster_sg.checkout.run_checkout`.
"""

from __future__ import annotations

import pytest

from src.utils import purchase_guard
from src.vendors.ticketmaster_sg import checkout as sg_checkout
from src.vendors.ticketmaster_sg.checkout import run_checkout

# Minimal HTML fragment exposing exactly the elements the SG checkout
# flow reads: a single delivery radio, a Place Order button, and a
# click counter the test can assert against.
_CHECKOUT_HTML = """
<!DOCTYPE html>
<html>
  <body>
    <fieldset id="deliveryFieldset">
      <div class="form-check delivery-option">
        <input id="delivery_mobile" type="radio"
               class="form-check-input delivery-radio"
               name="CheckoutForm[deliveryMethod]" value="mobile">
        <label class="form-check-label" for="delivery_mobile">
          Mobile Entry
        </label>
      </div>
    </fieldset>
    <div id="orderSummary">2 tickets - GENADM</div>
    <button id="placeOrderButton">Place Order</button>
    <script>
      window.__sgPlaceOrderClicks = 0;
      document.querySelector('#placeOrderButton').addEventListener(
        'click',
        function () { window.__sgPlaceOrderClicks += 1; },
        true,
      );
    </script>
  </body>
</html>
"""


async def _set_minimal_checkout(page) -> None:  # noqa: ANN001
    await page.set_content(_CHECKOUT_HTML)


async def _patch_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the captcha wait so the test never blocks on a human."""

    async def _no_captcha(page, *, timeout_seconds: float = 300.0) -> bool:  # noqa: ANN001
        return True

    monkeypatch.setattr(sg_checkout.sg_auth, "wait_for_human_if_captcha", _no_captcha)


async def _patch_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the SG terms-checkbox interaction for the minimal fixture."""

    async def _accept_terms(page) -> bool:  # noqa: ANN001
        return True

    monkeypatch.setattr(sg_checkout.sg_cart, "accept_terms_if_needed", _accept_terms)


# ---------------------------------------------------------------------------
# auto_purchase=False — Place Order never even attempted.
# ---------------------------------------------------------------------------


async def test_auto_purchase_false_never_clicks_place_order(
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
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0


# ---------------------------------------------------------------------------
# auto_purchase=True + guard NOT overridden → blocked, no click, soft fail.
# ---------------------------------------------------------------------------


async def test_auto_purchase_true_blocked_when_env_unset(
    chromium_context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(purchase_guard.PURCHASE_OVERRIDE_ENV, raising=False)
    await _patch_captcha(monkeypatch)
    await _patch_terms(monkeypatch)

    page = await chromium_context.new_page()
    await _set_minimal_checkout(page)

    with caplog.at_level("ERROR", logger="ticketmaster-bot"):
        result = await run_checkout(
            page,
            auto_purchase=True,
            card_last_four=None,
            action_delay=(0.0, 0.0),
            preferred_delivery=["mobile entry"],
            expected_quantity=0,  # bypass cart-match verification
            captcha_timeout_seconds=0.1,
        )

    # Guard short-circuits the place-order click and returns False as a
    # soft failure.
    assert result is False
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0
    # The guard logged the blocked message via the ticketmaster-bot logger.
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "BLOCKED" in joined or "blocked by purchase guard" in joined.lower()


async def test_auto_purchase_true_blocked_with_wrong_env_value(
    chromium_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(purchase_guard.PURCHASE_OVERRIDE_ENV, "nope")
    await _patch_captcha(monkeypatch)
    await _patch_terms(monkeypatch)

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
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 0


# ---------------------------------------------------------------------------
# auto_purchase=True + guard explicitly overridden → click proceeds.
# ---------------------------------------------------------------------------


async def test_auto_purchase_true_clicks_when_env_overridden(
    chromium_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        purchase_guard.PURCHASE_OVERRIDE_ENV,
        purchase_guard.PURCHASE_OVERRIDE_VALUE,
    )
    await _patch_captcha(monkeypatch)
    await _patch_terms(monkeypatch)

    page = await chromium_context.new_page()
    await _set_minimal_checkout(page)

    # The minimal fixture has no confirmation-page markers, so
    # run_checkout will return False from _verify_order_placed even
    # though the click went through. We assert on the click counter
    # rather than the return value here.
    await run_checkout(
        page,
        auto_purchase=True,
        card_last_four=None,
        action_delay=(0.0, 0.0),
        preferred_delivery=["mobile entry"],
        expected_quantity=0,
        captcha_timeout_seconds=0.1,
    )
    clicks = await page.evaluate("() => window.__sgPlaceOrderClicks")
    assert clicks == 1
