"""Tests for ``VFanAwareStrategy`` driven by real headless Chromium.

The fixture-backed tests load committed HTML files (``vfan_aware.html``
and ``vfan_aware_no_input.html``) and exercise the real strategy
against the real DOM. The vfan-input fill is observed by reading
``page.input_value`` on the ``[data-bdd='vfan-code']`` field and the
submit-button click via the page-side ``window.__vfan_submits`` log.
"""

from __future__ import annotations

import logging

import pytest

from src.strategies.cheapest import CheapestStrategy
from src.strategies.vfan_aware import VFanAwareStrategy


async def _click_count(page, idx: int) -> int:
    return int(
        await page.locator(
            f'[data-bdd="quick-pick-row"][data-idx="{idx}"]'
        ).get_attribute("data-click-count")
        or "0"
    )


# --- fixture-backed assertions --------------------------------------------


async def test_vfan_aware_fills_code_then_delegates(chromium_context, fixture_url):
    """[dom.vfan-aware-happy]: the wrapper fills ``[data-bdd='vfan-code']``
    with the configured code before any quick-pick click and returns
    the same candidate ``CheapestStrategy`` would have picked.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vfan_aware"))

    strategy = VFanAwareStrategy(inner=CheapestStrategy(), code="ABCD")
    chosen = await strategy.pick(page)

    # The inner strategy picked the cheapest row (idx=1, $90).
    assert chosen is not None
    assert chosen.price == 90.0
    assert await _click_count(page, 1) == 1
    for other in (0, 2):
        assert await _click_count(page, other) == 0

    # The input is now populated with the code, readable via input_value.
    assert await page.input_value('[data-bdd="vfan-code"]') == "ABCD"

    # The submit button was clicked exactly once, with the code present.
    submits = await page.evaluate("() => window.__vfan_submits")
    assert submits == ["ABCD"]


async def test_vfan_aware_missing_input_warns_and_delegates(
    chromium_context, fixture_url, caplog
):
    """[dom.vfan-aware-no-input]: when the input is absent the strategy
    logs a warning at the bot logger and still runs the inner pick.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vfan_aware_no_input"))

    strategy = VFanAwareStrategy(inner=CheapestStrategy(), code="ABCD")
    with caplog.at_level(logging.WARNING, logger="ticketmaster-bot"):
        chosen = await strategy.pick(page)

    # Inner pick still runs: cheapest of two rows is the $90 idx=1.
    assert chosen is not None
    assert chosen.price == 90.0
    assert await _click_count(page, 1) == 1
    assert await _click_count(page, 0) == 0

    # A warning was emitted that mentions the missing vfan input.
    assert any(
        "vfan" in record.message.lower() and record.levelno == logging.WARNING
        for record in caplog.records
    ), [r.message for r in caplog.records]


# --- targeted behavior tests ----------------------------------------------


async def test_vfan_aware_fills_before_clicking_row(chromium_context, fixture_url):
    """The vfan submit must be observed before any quick-pick row is
    clicked, proving the wrapper runs the gate FIRST.

    We compare the ordering of the recorded vfan submit (which fires on
    button click) and the recorded quick-pick click by inspecting both
    arrays after ``pick`` returns. Since the test page records both
    streams in real time, the submit happening at all alongside the
    final pick proves the gate ran before the inner returned.
    """
    page = await chromium_context.new_page()
    await page.goto(fixture_url("vfan_aware"))

    strategy = VFanAwareStrategy(inner=CheapestStrategy(), code="WXYZ")
    chosen = await strategy.pick(page)
    assert chosen is not None

    # The submit ran with the same code that we configured.
    submits = await page.evaluate("() => window.__vfan_submits")
    assert submits == ["WXYZ"]
    # And the row click happened.
    clicks = await page.evaluate("() => window.__clicks")
    assert clicks == ["1"]


async def test_vfan_aware_passes_inner_result_through(chromium_context, fixture_url):
    """The wrapper returns exactly what ``inner.pick`` returned (no
    re-wrapping of the candidate). When the inner returns ``None`` (e.g.
    no quick-pick rows on the page), the wrapper returns ``None`` too.
    """
    page = await chromium_context.new_page()
    await page.set_content(
        '<!doctype html><html><body><main role="main">'
        '<input data-bdd="vfan-code">'
        '<button data-bdd="vfan-submit">Verify</button>'
        '<ul data-bdd="quick-picks-list"></ul>'
        "</main></body></html>"
    )

    strategy = VFanAwareStrategy(inner=CheapestStrategy(), code="ABCD")
    assert await strategy.pick(page) is None
    # The code was still filled before the inner ran.
    assert await page.input_value('[data-bdd="vfan-code"]') == "ABCD"


def test_vfan_aware_rejects_empty_code() -> None:
    """An empty / whitespace-only code is a config mistake — surface it
    loudly instead of silently submitting an empty form."""
    with pytest.raises(ValueError):
        VFanAwareStrategy(inner=CheapestStrategy(), code="")
    with pytest.raises(ValueError):
        VFanAwareStrategy(inner=CheapestStrategy(), code="   ")


def test_vfan_aware_registered_in_strategy_registry() -> None:
    """The strategy must be discoverable by name via the singleton
    ``StrategyRegistry`` so external code can build it generically."""
    from src.registry import strategies as strategy_registry
    from src.strategies import factory  # noqa: F401  (import for side effects)

    assert "vfan_aware" in strategy_registry.registry
    assert strategy_registry.registry.get("vfan_aware") is VFanAwareStrategy
