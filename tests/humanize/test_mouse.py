"""Tests for ``humanize.mouse.bezier_move`` driven by real headless Chromium.

The function must walk the cursor between two points along a cubic Bezier
curve, issuing many small ``page.mouse.move`` calls instead of a single
jump. Each test wraps the real ``page.mouse.move`` with a recording proxy
and asserts on the captured move events.
"""

from __future__ import annotations

import math

import pytest
from playwright.async_api import BrowserContext

from src.humanize.mouse import bezier_move


def _wrap_mouse_move(page) -> list[tuple[float, float]]:  # noqa: ANN001
    """Replace ``page.mouse.move`` with a recorder that also forwards.

    Returns the list the recorder appends ``(x, y)`` tuples to. The
    original ``page.mouse.move`` coroutine is still invoked so the cursor
    really moves inside the browser.
    """
    captured: list[tuple[float, float]] = []
    original = page.mouse.move

    async def recording_move(x: float, y: float, **kwargs):  # noqa: ANN001
        captured.append((float(x), float(y)))
        return await original(x, y, **kwargs)

    page.mouse.move = recording_move  # type: ignore[method-assign]
    return captured


async def _new_blank_page(context: BrowserContext):  # noqa: ANN202
    page = await context.new_page()
    await page.set_content(
        '<!doctype html><html><body><main style="height:600px;width:800px;">'
        "  bezier-move target"
        "</main></body></html>"
    )
    return page


# --- core behaviour --------------------------------------------------------


async def test_bezier_move_records_at_least_10_distinct_moves(chromium_context):
    """[dom.humanize-mouse-bezier]: many distinct ``page.mouse.move`` events."""
    page = await _new_blank_page(chromium_context)
    captured = _wrap_mouse_move(page)

    await bezier_move(page, (0.0, 0.0), (500.0, 400.0))

    assert len(captured) >= 10
    # The fixture-stage move count must be distinct intermediate points,
    # not a single jump duplicated. Allow tiny duplicates from rounding.
    unique = {(round(x, 1), round(y, 1)) for x, y in captured}
    assert len(unique) >= 10


async def test_bezier_move_ends_at_target(chromium_context):
    page = await _new_blank_page(chromium_context)
    captured = _wrap_mouse_move(page)

    end_x, end_y = 500.0, 400.0
    await bezier_move(page, (0.0, 0.0), (end_x, end_y))

    assert captured, "expected at least one move"
    last_x, last_y = captured[-1]
    assert math.isclose(last_x, end_x, abs_tol=1.0)
    assert math.isclose(last_y, end_y, abs_tol=1.0)


async def test_bezier_move_path_stays_between_endpoints_reasonably(chromium_context):
    """Coordinates should remain inside an inflated bounding box of the
    endpoints, allowing for the natural control-point detour.
    """
    page = await _new_blank_page(chromium_context)
    captured = _wrap_mouse_move(page)

    await bezier_move(page, (50.0, 50.0), (400.0, 300.0))

    # Allow up to 200px outside the rectangle for the bezier control bulge.
    xs = [p[0] for p in captured]
    ys = [p[1] for p in captured]
    assert min(xs) >= 50.0 - 200.0
    assert max(xs) <= 400.0 + 200.0
    assert min(ys) >= 50.0 - 200.0
    assert max(ys) <= 300.0 + 200.0


async def test_bezier_move_explicit_steps_param(chromium_context):
    page = await _new_blank_page(chromium_context)
    captured = _wrap_mouse_move(page)

    await bezier_move(page, (10.0, 10.0), (310.0, 210.0), steps=30, delay_ms=0)

    # Exactly 30 intermediate move events when steps is fixed.
    assert len(captured) == 30


async def test_bezier_move_steps_min_max_overrides(chromium_context):
    """Passing min/max should bound the random step count."""
    page = await _new_blank_page(chromium_context)
    captured = _wrap_mouse_move(page)

    await bezier_move(
        page,
        (0.0, 0.0),
        (200.0, 200.0),
        steps_min=25,
        steps_max=25,
        delay_ms=0,
    )
    assert len(captured) == 25


async def test_bezier_move_rejects_invalid_steps(chromium_context):
    page = await _new_blank_page(chromium_context)
    with pytest.raises(ValueError):
        await bezier_move(page, (0.0, 0.0), (100.0, 100.0), steps=0)


async def test_bezier_move_rejects_invalid_delay_range(chromium_context):
    page = await _new_blank_page(chromium_context)
    with pytest.raises(ValueError):
        await bezier_move(
            page,
            (0.0, 0.0),
            (100.0, 100.0),
            steps=10,
            delay_ms_min=20,
            delay_ms_max=5,
        )


# --- config block ----------------------------------------------------------


def test_config_humanize_mouse_block_has_expected_keys(tmp_path):
    """The mouse humanize block must round-trip through ``load_config``."""
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        timing:
          humanize:
            enabled: true
            mouse:
              enabled: true
              steps_min: 22
              steps_max: 38
              delay_ms_min: 9
              delay_ms_max: 24
        """
    )

    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    mouse = cfg.timing.humanize.mouse
    assert cfg.timing.humanize.enabled is True
    assert mouse.enabled is True
    assert mouse.steps_min == 22
    assert mouse.steps_max == 38
    assert mouse.delay_ms_min == 9
    assert mouse.delay_ms_max == 24


def test_config_humanize_accepts_legacy_bool(tmp_path):
    """Old-style ``timing.humanize: true`` must still load."""
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        timing:
          humanize: true
        """
    )

    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    # Legacy boolean form maps to enabled with default mouse settings.
    assert cfg.timing.humanize.enabled is True
    assert cfg.timing.humanize.mouse.steps_min > 0
    assert cfg.timing.humanize.mouse.delay_ms_max > 0
