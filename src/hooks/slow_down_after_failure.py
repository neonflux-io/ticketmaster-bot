"""Hook that bumps the runner's action-delay window after a failure.

Failures often arrive because the bot is being rate-limited, captcha-gated,
or otherwise pushed back. :class:`SlowDownAfterFailureHook` reacts to the
``on_failure`` lifecycle event by multiplying the runner's
``action_delay_min`` and ``action_delay_max`` by a configurable factor
(default ``1.5``) so subsequent attempts pace themselves more cautiously.

The hook is deliberately conservative:

* It only mutates the two attributes documented in
  :class:`~src.vendors.ticketmaster.core.BotRunner`. Anything else on the
  ``ctx`` is left alone.
* If ``ctx`` is ``None`` or does not expose both attributes, the hook
  silently returns: failure handling must never raise from a hook.
* The bump is *cumulative*. Three consecutive failures at multiplier
  ``2.0`` drive the delays up by ``8x``. This matches the intuition that
  repeated failures call for ever-longer back-off rather than a single
  flat penalty.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Hook

log = logging.getLogger("ticketmaster-bot")

DEFAULT_MULTIPLIER: float = 1.5


class SlowDownAfterFailureHook(Hook):
    """Multiply the runner's action-delay window on every ``on_failure`` event.

    Parameters
    ----------
    multiplier:
        Strict-positive multiplicand applied to both
        ``action_delay_min`` and ``action_delay_max``. Defaults to
        :data:`DEFAULT_MULTIPLIER` (``1.5``).

    Raises
    ------
    ValueError:
        If ``multiplier`` is not strictly positive (``<= 0``). A
        non-positive multiplier would either freeze or *decrease* the
        delay, neither of which matches the documented intent.
    """

    #: Default class-level priority. Slow-down should fire *before* any
    #: observer-style hook that snapshots the runner state, so we set a
    #: small negative priority. The dispatcher uses ascending order.
    priority: int = -10

    def __init__(self, multiplier: float = DEFAULT_MULTIPLIER) -> None:
        if multiplier <= 0:
            raise ValueError(f"SlowDownAfterFailureHook.multiplier must be > 0, got {multiplier!r}")
        self.multiplier = float(multiplier)

    async def on_event(self, ctx: Any, event_type: str, **kwargs: Any) -> None:
        if event_type != "on_failure":
            return
        if ctx is None:
            log.debug("SlowDownAfterFailureHook: ctx is None; nothing to slow down")
            return
        if not (hasattr(ctx, "action_delay_min") and hasattr(ctx, "action_delay_max")):
            log.debug(
                "SlowDownAfterFailureHook: ctx %r lacks action_delay_min/max; skipping",
                type(ctx).__name__,
            )
            return

        try:
            new_min = float(ctx.action_delay_min) * self.multiplier
            new_max = float(ctx.action_delay_max) * self.multiplier
        except (TypeError, ValueError):
            log.warning(
                "SlowDownAfterFailureHook: non-numeric action_delay on %r; skipping",
                type(ctx).__name__,
            )
            return

        log.info(
            "Slowing down after failure: action_delay (%.3f, %.3f) -> (%.3f, %.3f) (x%.2f)",
            ctx.action_delay_min,
            ctx.action_delay_max,
            new_min,
            new_max,
            self.multiplier,
        )
        ctx.action_delay_min = new_min
        ctx.action_delay_max = new_max


__all__ = ["DEFAULT_MULTIPLIER", "SlowDownAfterFailureHook"]
