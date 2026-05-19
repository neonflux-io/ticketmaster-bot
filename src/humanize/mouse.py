"""Human-cadence mouse movement helpers.

The single public entry point is :func:`bezier_move`. It walks the cursor
from ``start_xy`` to ``end_xy`` along a cubic Bezier curve, emitting one
``page.mouse.move`` call per step with a small randomised delay between
moves. The randomised control points produce a gentle, non-linear path
that looks like a hand-driven cursor rather than the straight teleport
Playwright produces by default.

Two control points are chosen at random within a bounding region around
the straight segment so consecutive runs trace different paths. ``steps``
and ``delay_ms`` may be given as exact values or as ``_min`` / ``_max``
ranges for further per-call randomisation.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

# Tuned to match the F4.1 spec: 20-40 steps, 8-25 ms per step.
DEFAULT_STEPS_MIN = 20
DEFAULT_STEPS_MAX = 40
DEFAULT_DELAY_MS_MIN = 8
DEFAULT_DELAY_MS_MAX = 25
DEFAULT_CONTROL_POINTS = 2


def _cubic_bezier_point(
    t: float,
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[float, float]:
    """Return the point on a cubic Bezier at parameter ``t in [0, 1]``."""
    one_minus_t = 1.0 - t
    b0 = one_minus_t**3
    b1 = 3 * one_minus_t**2 * t
    b2 = 3 * one_minus_t * t**2
    b3 = t**3
    x = b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0]
    y = b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1]
    return x, y


def _random_control_point(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    rng: random.Random,
    fraction: tuple[float, float],
    jitter: float,
) -> tuple[float, float]:
    """Pick a control point near the line segment but laterally offset.

    The point is placed at ``fraction`` of the way from start to end,
    then offset by a random vector up to ``jitter`` pixels on each axis.
    The jitter is bounded so the path stays inside a reasonable bounding
    box around the segment.
    """
    f = rng.uniform(*fraction)
    base_x = start[0] + (end[0] - start[0]) * f
    base_y = start[1] + (end[1] - start[1]) * f
    offset_x = rng.uniform(-jitter, jitter)
    offset_y = rng.uniform(-jitter, jitter)
    return base_x + offset_x, base_y + offset_y


def _resolve_step_count(
    *,
    steps: int | None,
    steps_min: int | None,
    steps_max: int | None,
    rng: random.Random,
) -> int:
    """Resolve the number of intermediate move events to emit."""
    if steps is not None:
        if steps < 1:
            raise ValueError(f"bezier_move steps must be >= 1, got {steps}")
        return int(steps)
    lo = DEFAULT_STEPS_MIN if steps_min is None else int(steps_min)
    hi = DEFAULT_STEPS_MAX if steps_max is None else int(steps_max)
    if lo < 1 or hi < lo:
        raise ValueError(f"bezier_move steps_min/steps_max invalid: min={lo} max={hi}")
    return rng.randint(lo, hi)


def _resolve_delay_range(
    *,
    delay_ms: int | None,
    delay_ms_min: int | None,
    delay_ms_max: int | None,
) -> tuple[int, int]:
    """Resolve the (min, max) ms delay between moves."""
    if delay_ms is not None:
        if delay_ms < 0:
            raise ValueError(f"bezier_move delay_ms must be >= 0, got {delay_ms}")
        return int(delay_ms), int(delay_ms)
    lo = DEFAULT_DELAY_MS_MIN if delay_ms_min is None else int(delay_ms_min)
    hi = DEFAULT_DELAY_MS_MAX if delay_ms_max is None else int(delay_ms_max)
    if lo < 0 or hi < lo:
        raise ValueError(f"bezier_move delay_ms_min/delay_ms_max invalid: min={lo} max={hi}")
    return lo, hi


async def bezier_move(
    page: Page,
    start_xy: tuple[float, float],
    end_xy: tuple[float, float],
    *,
    control_points: int = DEFAULT_CONTROL_POINTS,
    steps: int | None = None,
    steps_min: int | None = None,
    steps_max: int | None = None,
    delay_ms: int | None = None,
    delay_ms_min: int | None = None,
    delay_ms_max: int | None = None,
    seed: int | None = None,
) -> None:
    """Move the mouse from ``start_xy`` to ``end_xy`` along a cubic Bezier.

    Parameters
    ----------
    page:
        The Playwright page whose mouse we drive.
    start_xy, end_xy:
        Source and destination viewport coordinates.
    control_points:
        Number of intermediate Bezier control points. Currently only ``2``
        is supported (cubic Bezier); anything else is rejected so the
        caller notices instead of silently degrading.
    steps:
        Exact number of move events to emit. Mutually exclusive with
        ``steps_min`` / ``steps_max`` (which produce a random count in
        that inclusive range).
    delay_ms:
        Exact delay in milliseconds between consecutive moves. Mutually
        exclusive with ``delay_ms_min`` / ``delay_ms_max`` (which produce
        a random delay per step).
    seed:
        Optional RNG seed for deterministic tests.
    """
    if control_points != 2:
        # The function name implies a cubic Bezier; we keep the API
        # explicit instead of silently swapping to a different curve.
        raise ValueError(f"bezier_move currently supports control_points=2, got {control_points}")

    rng = random.Random(seed) if seed is not None else random.Random()

    step_count = _resolve_step_count(
        steps=steps,
        steps_min=steps_min,
        steps_max=steps_max,
        rng=rng,
    )
    min_delay_ms, max_delay_ms = _resolve_delay_range(
        delay_ms=delay_ms,
        delay_ms_min=delay_ms_min,
        delay_ms_max=delay_ms_max,
    )

    # Jitter is proportional to the segment length so very short moves
    # don't overshoot wildly and long moves don't look mechanical.
    dx = end_xy[0] - start_xy[0]
    dy = end_xy[1] - start_xy[1]
    length = max(1.0, (dx * dx + dy * dy) ** 0.5)
    jitter = max(8.0, min(length * 0.25, 120.0))

    cp1 = _random_control_point(start_xy, end_xy, rng=rng, fraction=(0.2, 0.45), jitter=jitter)
    cp2 = _random_control_point(start_xy, end_xy, rng=rng, fraction=(0.55, 0.8), jitter=jitter)

    # Emit step_count distinct intermediate points landing exactly at
    # end_xy on the final iteration.
    for i in range(1, step_count + 1):
        t = i / step_count
        x, y = _cubic_bezier_point(t, start_xy, cp1, cp2, end_xy)
        await page.mouse.move(x, y)
        if i == step_count:
            break
        if max_delay_ms > 0:
            delay = rng.randint(min_delay_ms, max_delay_ms)
            if delay > 0:
                await asyncio.sleep(delay / 1000.0)


__all__ = ["bezier_move"]
