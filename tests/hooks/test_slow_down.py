"""Tests for :class:`SlowDownAfterFailureHook`.

Covers the contract in [io.slowdown-after-failure-bumps-delay]: after a
failure event fires, ``ctx.action_delay_min`` and ``ctx.action_delay_max``
must be strictly greater than their pre-failure values. Verified against
both a minimal stand-in object (carrying just the two attributes) and a
real :class:`~src.vendors.ticketmaster.core.BotRunner`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.hooks.base import Hook, HookRegistry
from src.hooks.slow_down_after_failure import SlowDownAfterFailureHook
from src.orchestrator.lifecycle import LifecycleDispatcher
from src.utils.config_loader import (
    AccountConfig,
    BotConfig,
    BrowserConfig,
    CheckoutConfig,
    EventConfig,
    LoggingConfig,
    NotificationsConfig,
    PaymentConfig,
    ProxyConfig,
    StealthConfig,
    TicketsConfig,
    TimingConfig,
)
from src.vendors.ticketmaster.core import BotRunner


class _DelayCarrier:
    """Minimal object exposing the two attributes the hook mutates."""

    def __init__(self, lo: float, hi: float) -> None:
        self.action_delay_min = lo
        self.action_delay_max = hi


def test_slow_down_default_multiplier_is_one_point_five() -> None:
    hook = SlowDownAfterFailureHook()
    assert hook.multiplier == pytest.approx(1.5)


def test_slow_down_is_a_hook() -> None:
    """The hook subclasses :class:`Hook` so it plugs into a HookRegistry."""
    assert issubclass(SlowDownAfterFailureHook, Hook)


def test_slow_down_rejects_non_positive_multipliers() -> None:
    """A non-positive multiplier would *decrease* delays - reject at construction."""
    with pytest.raises(ValueError):
        SlowDownAfterFailureHook(multiplier=0.0)
    with pytest.raises(ValueError):
        SlowDownAfterFailureHook(multiplier=-1.0)


async def test_slow_down_multiplies_action_delay_on_failure() -> None:
    """``on_failure`` multiplies both attributes by the configured multiplier."""
    runner = _DelayCarrier(0.5, 2.0)
    hook = SlowDownAfterFailureHook(multiplier=1.5)
    await hook.on_event(runner, "on_failure", error=RuntimeError("boom"))
    assert runner.action_delay_min == pytest.approx(0.75)
    assert runner.action_delay_max == pytest.approx(3.0)


async def test_slow_down_supports_custom_multiplier() -> None:
    runner = _DelayCarrier(1.0, 4.0)
    hook = SlowDownAfterFailureHook(multiplier=2.0)
    await hook.on_event(runner, "on_failure")
    assert runner.action_delay_min == pytest.approx(2.0)
    assert runner.action_delay_max == pytest.approx(8.0)


async def test_slow_down_only_fires_on_failure_event() -> None:
    """Other lifecycle events leave the delays untouched."""
    runner = _DelayCarrier(0.5, 2.0)
    hook = SlowDownAfterFailureHook(multiplier=3.0)
    for event in (
        "on_run_start",
        "before_login",
        "after_login",
        "before_navigate",
        "after_queue_release",
        "before_select",
        "after_select",
        "before_cart",
        "after_cart",
        "before_checkout",
        "before_place_order",
        "on_run_end",
    ):
        await hook.on_event(runner, event)
    assert runner.action_delay_min == pytest.approx(0.5)
    assert runner.action_delay_max == pytest.approx(2.0)


async def test_slow_down_accumulates_across_repeated_failures() -> None:
    """Each failure multiplies again - the bump is *cumulative*."""
    runner = _DelayCarrier(0.5, 2.0)
    hook = SlowDownAfterFailureHook(multiplier=2.0)
    await hook.on_event(runner, "on_failure")
    await hook.on_event(runner, "on_failure")
    await hook.on_event(runner, "on_failure")
    # 0.5 * 2^3 = 4.0; 2.0 * 2^3 = 16.0
    assert runner.action_delay_min == pytest.approx(4.0)
    assert runner.action_delay_max == pytest.approx(16.0)


async def test_slow_down_strictly_increases_delays_after_failure() -> None:
    """The strict-greater-than guarantee that backs the validation assertion."""
    runner = _DelayCarrier(0.25, 1.0)
    before_min = runner.action_delay_min
    before_max = runner.action_delay_max
    hook = SlowDownAfterFailureHook(multiplier=1.5)
    await hook.on_event(runner, "on_failure")
    assert runner.action_delay_min > before_min
    assert runner.action_delay_max > before_max


async def test_slow_down_tolerates_missing_attributes_without_raising() -> None:
    """When ``ctx`` is None or lacks delay attributes, the hook is a no-op."""
    hook = SlowDownAfterFailureHook(multiplier=1.5)
    await hook.on_event(None, "on_failure", error=RuntimeError("boom"))

    class _NoDelays:
        pass

    await hook.on_event(_NoDelays(), "on_failure", error=RuntimeError("boom"))


def _make_minimal_bot_config(*, user_data_dir: Path) -> BotConfig:
    return BotConfig(
        events=[EventConfig(url="https://www.ticketmaster.com/event/X")],
        tickets=TicketsConfig(),
        checkout=CheckoutConfig(payment=PaymentConfig()),
        timing=TimingConfig(),
        logging=LoggingConfig(),
        notifications=NotificationsConfig(),
        browser=BrowserConfig(
            headless=True,
            user_data_dir=str(user_data_dir),
            stealth=StealthConfig(enabled=False),
        ),
        accounts=[AccountConfig(email="alice@example.com", password="x", name="alice")],
        proxy=ProxyConfig(enabled=False, policy="sticky", urls=[]),
    )


async def test_slow_down_applies_to_real_botrunner(tmp_path: Path) -> None:
    """End-to-end: the hook bumps a real BotRunner's action delays."""
    cfg = _make_minimal_bot_config(user_data_dir=tmp_path / "default")
    runner = BotRunner(cfg)
    hook = SlowDownAfterFailureHook(multiplier=1.5)
    runner.hooks.register(hook)

    before_min = runner.action_delay_min
    before_max = runner.action_delay_max
    await runner.lifecycle.fire(
        "on_failure",
        runner,
        error=RuntimeError("forced"),
        account=runner.account.name if runner.account else None,
    )
    assert runner.action_delay_min == pytest.approx(before_min * 1.5)
    assert runner.action_delay_max == pytest.approx(before_max * 1.5)


async def test_slow_down_works_inside_dispatcher() -> None:
    """The hook fires through :class:`LifecycleDispatcher` exactly like any other hook."""
    runner = _DelayCarrier(0.5, 2.0)
    reg = HookRegistry()
    reg.register(SlowDownAfterFailureHook(multiplier=1.5))
    dispatcher = LifecycleDispatcher(reg)
    await dispatcher.fire("on_failure", runner, error=RuntimeError("boom"))
    assert runner.action_delay_min == pytest.approx(0.75)
    assert runner.action_delay_max == pytest.approx(3.0)
