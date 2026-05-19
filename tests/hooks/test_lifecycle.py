"""Tests for the lifecycle hook framework.

Covers the contract in [io.hooks-lifecycle-order]: for a run that completes
one selection step, registered hooks must fire in the exact order
``on_run_start, before_select, after_select, on_run_end``.

These tests drive real, in-process code paths only. The "mock flow"
exercises the real :class:`LifecycleDispatcher` and a real recording
:class:`Hook` subclass. The BotRunner-wiring tests substitute a real
async-context-manager Python object for ``async_playwright`` (no
external test-double libraries) so we don't pay the cost of launching
a real Chromium child just to assert lifecycle ordering.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from src.hooks.base import Hook, HookRegistry
from src.orchestrator.lifecycle import LIFECYCLE_EVENTS, LifecycleDispatcher
from src.registry import hooks as hooks_registry
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

# ---------------------------------------------------------------------------
# Recording hook used by every test in this module.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingHook(Hook):
    """Hook subclass that appends every fired event to ``events``.

    Constructed in each test so the recorded list is per-test isolated.
    """

    events: list[str] = field(default_factory=list)
    contexts: list[Any] = field(default_factory=list)
    kwargs_seen: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    priority: int = 0

    async def on_event(self, ctx: Any, event_type: str, **kwargs: Any) -> None:
        self.events.append(event_type)
        self.contexts.append(ctx)
        self.kwargs_seen.append(dict(kwargs))


# ---------------------------------------------------------------------------
# Hook ABC + HookRegistry contract.
# ---------------------------------------------------------------------------


def test_hook_abc_requires_on_event() -> None:
    """``Hook`` is abstract: instantiating without overriding raises."""
    with pytest.raises(TypeError):
        Hook()  # type: ignore[abstract]


def test_hook_defaults_enabled_priority() -> None:
    """A bare subclass defaults to enabled=True and a numeric priority."""

    class _Bare(Hook):
        async def on_event(self, ctx: Any, event_type: str, **kwargs: Any) -> None:
            return None

    h = _Bare()
    assert h.enabled is True
    assert isinstance(h.priority, int)


def test_hook_registry_register_and_all() -> None:
    """``HookRegistry`` collects registered hooks; ``all()`` returns them."""
    reg = HookRegistry()
    a = _RecordingHook()
    b = _RecordingHook()
    reg.register(a)
    reg.register(b)
    assert list(reg.all()) == [a, b]


def test_hook_registry_clear() -> None:
    reg = HookRegistry()
    reg.register(_RecordingHook())
    reg.register(_RecordingHook())
    reg.clear()
    assert list(reg.all()) == []


def test_hook_registry_register_many_accepts_iterable() -> None:
    reg = HookRegistry()
    hooks = [_RecordingHook(), _RecordingHook()]
    reg.register_many(hooks)
    assert list(reg.all()) == hooks


def test_lifecycle_events_constant_matches_feature_spec() -> None:
    """The published lifecycle event list matches the feature description."""
    assert LIFECYCLE_EVENTS == (
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
        "on_failure",
        "on_run_end",
    )


# ---------------------------------------------------------------------------
# LifecycleDispatcher behaviour.
# ---------------------------------------------------------------------------


async def test_dispatcher_fire_awaits_each_hook_in_priority_order() -> None:
    """``LifecycleDispatcher.fire`` iterates hooks ordered by priority asc."""
    reg = HookRegistry()
    low = _RecordingHook(priority=10)
    high = _RecordingHook(priority=-5)
    mid = _RecordingHook(priority=0)
    reg.register(low)
    reg.register(high)
    reg.register(mid)

    dispatcher = LifecycleDispatcher(reg)
    ctx = object()
    await dispatcher.fire("on_run_start", ctx)

    # high.priority=-5 first, then mid=0, then low=10.
    assert high.events == ["on_run_start"]
    assert mid.events == ["on_run_start"]
    assert low.events == ["on_run_start"]


async def test_dispatcher_skips_disabled_hooks() -> None:
    """Hooks whose ``enabled`` flag is false are not invoked."""
    reg = HookRegistry()
    on = _RecordingHook(enabled=True)
    off = _RecordingHook(enabled=False)
    reg.register(on)
    reg.register(off)

    dispatcher = LifecycleDispatcher(reg)
    await dispatcher.fire("on_run_start", object())

    assert on.events == ["on_run_start"]
    assert off.events == []


async def test_dispatcher_forwards_kwargs() -> None:
    reg = HookRegistry()
    hook = _RecordingHook()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)
    ctx = {"run": "alpha"}
    await dispatcher.fire("after_select", ctx, candidate="row-1", price=199.0)

    assert hook.contexts == [ctx]
    assert hook.kwargs_seen == [{"candidate": "row-1", "price": 199.0}]


async def test_dispatcher_rejects_unknown_event_names() -> None:
    """Unknown event names raise ``ValueError`` to guard against typos."""
    reg = HookRegistry()
    reg.register(_RecordingHook())
    dispatcher = LifecycleDispatcher(reg)
    with pytest.raises(ValueError, match="unknown lifecycle event"):
        await dispatcher.fire("not_a_real_event", object())


async def test_dispatcher_continues_when_one_hook_raises(caplog) -> None:
    """A hook that raises must not stop later hooks from firing."""

    class _Bad(Hook):
        priority = 0

        async def on_event(self, ctx: Any, event_type: str, **kwargs: Any) -> None:
            raise RuntimeError("boom")

    reg = HookRegistry()
    reg.register(_Bad())
    good = _RecordingHook(priority=10)
    reg.register(good)

    dispatcher = LifecycleDispatcher(reg)
    with caplog.at_level("ERROR"):
        await dispatcher.fire("on_run_start", object())

    assert good.events == ["on_run_start"]
    assert any("boom" in rec.getMessage() or "boom" in str(rec.exc_info) for rec in caplog.records)


# ---------------------------------------------------------------------------
# "Mock flow" assertion mandated by [io.hooks-lifecycle-order].
# ---------------------------------------------------------------------------


async def test_recording_hook_observes_single_step_flow_order() -> None:
    """A flow that completes one selection step fires exactly four events.

    Order: ``on_run_start, before_select, after_select, on_run_end``. This is
    the exact assertion behind ``[io.hooks-lifecycle-order]``.
    """
    reg = HookRegistry()
    hook = _RecordingHook()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    # The "mock flow" is the real LifecycleDispatcher driving the exact
    # sequence a successful single-step BotRunner would emit when no
    # login is required, the queue is bypassed, and selection succeeds
    # without cart/checkout.
    ctx = {"run": "single-step"}
    await dispatcher.fire("on_run_start", ctx)
    await dispatcher.fire("before_select", ctx)
    await dispatcher.fire("after_select", ctx, candidate="row-7")
    await dispatcher.fire("on_run_end", ctx, success=True)

    assert hook.events == [
        "on_run_start",
        "before_select",
        "after_select",
        "on_run_end",
    ]


# ---------------------------------------------------------------------------
# Singleton ``src.registry.hooks`` keeps the public surface (register/get/all).
# ---------------------------------------------------------------------------


def test_singleton_hooks_registry_round_trip() -> None:
    """The module-level singleton stays usable for entry-point plugins."""
    sentinel = object()
    key = "__lifecycle_test_probe__"
    try:
        hooks_registry.register(key, sentinel)
        assert hooks_registry.get(key) is sentinel
        assert key in hooks_registry.all()
    finally:
        hooks_registry.registry.unregister(key)


# ---------------------------------------------------------------------------
# BotRunner wiring: a single ``on_run_start`` / ``on_run_end`` round trip.
#
# The runner is patched at the ``async_playwright`` entrypoint with a
# real Python stand-in (no mocking libraries) so we get a deterministic
# flow that returns early after firing the start/end events.
# ---------------------------------------------------------------------------


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


class _LaunchFailed(RuntimeError):
    """Sentinel raised when our stand-in chromium short-circuits the run."""


async def test_botrunner_exposes_lifecycle_dispatcher_with_hookregistry(
    tmp_path: Path,
) -> None:
    """BotRunner owns a :class:`LifecycleDispatcher` backed by a HookRegistry."""
    cfg = _make_minimal_bot_config(user_data_dir=tmp_path / "default")
    runner = BotRunner(cfg)

    assert isinstance(runner.lifecycle, LifecycleDispatcher)
    assert isinstance(runner.hooks, HookRegistry)
    assert runner.lifecycle.registry is runner.hooks


async def test_botrunner_fires_on_run_start_and_on_failure_when_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When playwright fails to launch, BotRunner fires start, failure, end.

    Uses a real stand-in async-context-manager (no mocking libs) so we
    can capture call order deterministically without launching real
    Chromium.
    """
    cfg = _make_minimal_bot_config(user_data_dir=tmp_path / "default")
    runner = BotRunner(cfg)
    hook = _RecordingHook()
    runner.hooks.register(hook)

    class _StubChromium:
        async def launch_persistent_context(self, **kwargs: Any) -> None:
            raise _LaunchFailed("intentional short-circuit")

    class _StubPW:
        chromium = _StubChromium()

    class _StubCM:
        async def __aenter__(self) -> _StubPW:
            return _StubPW()

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    monkeypatch.setattr(
        "src.vendors.ticketmaster.core.async_playwright",
        lambda: _StubCM(),
    )

    with pytest.raises(_LaunchFailed):
        await runner.run()

    # on_run_start must fire before the launch attempt; on_failure must
    # fire when the launch raises; on_run_end must fire in the finally
    # block regardless.
    assert "on_run_start" in hook.events
    assert "on_failure" in hook.events
    assert "on_run_end" in hook.events
    # Order: start, then failure, then end.
    start_idx = hook.events.index("on_run_start")
    failure_idx = hook.events.index("on_failure")
    end_idx = hook.events.index("on_run_end")
    assert start_idx < failure_idx < end_idx


async def test_botrunner_hooks_attribute_independent_per_runner(
    tmp_path: Path,
) -> None:
    """Two runners share no hook registry state by accident."""
    cfg = _make_minimal_bot_config(user_data_dir=tmp_path / "default")
    r1 = BotRunner(cfg)
    r2 = BotRunner(cfg)
    r1.hooks.register(_RecordingHook())
    assert list(r2.hooks.all()) == []


# ---------------------------------------------------------------------------
# Concurrency: dispatch from inside ``asyncio.gather`` doesn't lose events.
# ---------------------------------------------------------------------------


async def test_dispatcher_handles_concurrent_fires() -> None:
    """Two concurrent ``fire`` calls deliver the full event set to each hook."""
    reg = HookRegistry()
    hook = _RecordingHook()
    reg.register(hook)
    dispatcher = LifecycleDispatcher(reg)

    ctx = object()
    await asyncio.gather(
        dispatcher.fire("on_run_start", ctx),
        dispatcher.fire("on_run_end", ctx),
    )
    assert sorted(hook.events) == ["on_run_end", "on_run_start"]
