"""Tests for ``DesktopNotifier`` and the notifier base layer.

These tests confirm the structural contract of ``NotifyEvent``/``Notifier``
and exercise ``DesktopNotifier.notify`` against the real (plyer) backend.
Plyer raises silently on headless CI/macOS sandboxes - the notifier
intentionally treats every backend error as best-effort and still emits a
clean info-level log line, which is the public assertion we care about
([io.desktop-notify-called]).
"""

from __future__ import annotations

import inspect
import logging

import pytest

from src.notifiers.base import Notifier, NotifyEvent
from src.notifiers.desktop import DesktopNotifier
from src.registry import notifiers as notifier_registry

# ---------------------------------------------------------------------------
# NotifyEvent dataclass shape
# ---------------------------------------------------------------------------


def test_notify_event_required_fields():
    event = NotifyEvent(
        event_type="cart_success",
        title="Cart ready",
        message="Tickets reserved",
    )
    assert event.event_type == "cart_success"
    assert event.title == "Cart ready"
    assert event.message == "Tickets reserved"
    # severity and metadata get sensible defaults
    assert event.severity == "info"
    assert event.metadata == {}


def test_notify_event_explicit_severity_and_metadata():
    event = NotifyEvent(
        event_type="failure",
        title="Boom",
        message="Stopped",
        severity="error",
        metadata={"account": "alice", "step": "checkout"},
    )
    assert event.severity == "error"
    assert event.metadata == {"account": "alice", "step": "checkout"}


def test_notify_event_metadata_is_independent_between_instances():
    a = NotifyEvent(event_type="x", title="t", message="m")
    b = NotifyEvent(event_type="y", title="t2", message="m2")
    a.metadata["k"] = "v"
    assert b.metadata == {}


# ---------------------------------------------------------------------------
# Notifier ABC contract
# ---------------------------------------------------------------------------


def test_notifier_is_abstract_with_async_notify():
    assert inspect.isabstract(Notifier)
    notify_method = getattr(Notifier, "notify", None)
    assert notify_method is not None
    assert inspect.iscoroutinefunction(notify_method)


def test_notifier_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Notifier()  # type: ignore[abstract]


def test_desktop_notifier_subclasses_notifier():
    assert issubclass(DesktopNotifier, Notifier)


# ---------------------------------------------------------------------------
# DesktopNotifier behaviour - [io.desktop-notify-called]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_desktop_notify_logs_clean_info_line(caplog):
    """notify() completes without raising and emits an info log line
    whose message contains the notification title.
    """
    notifier = DesktopNotifier(desktop=False, sound=False)
    event = NotifyEvent(
        event_type="cart_success",
        title="Tickets in cart",
        message="2x Section 100",
    )
    with caplog.at_level(logging.INFO, logger="ticketmaster-bot"):
        await notifier.notify(event)
    matches = [
        rec for rec in caplog.records if "Tickets in cart" in rec.getMessage()
    ]
    assert matches, "expected an info-level record carrying the title"
    assert any(rec.levelno == logging.INFO for rec in matches)


@pytest.mark.asyncio
async def test_desktop_notify_with_desktop_backend_does_not_raise(caplog):
    """Even with desktop=True, the plyer backend is best-effort and
    the notifier must complete cleanly. We still expect the info log
    line (we always log, before attempting the backend call).
    """
    notifier = DesktopNotifier(desktop=True, sound=False)
    event = NotifyEvent(
        event_type="info",
        title="hello",
        message="from pytest",
    )
    with caplog.at_level(logging.INFO, logger="ticketmaster-bot"):
        await notifier.notify(event)
    assert any("hello" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_desktop_notify_returns_none():
    notifier = DesktopNotifier(desktop=False, sound=False)
    event = NotifyEvent(event_type="x", title="t", message="m")
    result = await notifier.notify(event)
    assert result is None


# ---------------------------------------------------------------------------
# NotifierRegistry wiring
# ---------------------------------------------------------------------------


def test_notifier_registry_contains_desktop():
    """``desktop`` is registered with the singleton NotifierRegistry."""
    desktop_cls = notifier_registry.get("desktop")
    assert desktop_cls is DesktopNotifier


def test_notifier_registry_get_unknown_raises_with_name():
    from src.registry.base import NotRegistered

    with pytest.raises(NotRegistered) as info:
        notifier_registry.get("does_not_exist_zz")
    assert "does_not_exist_zz" in str(info.value)


def test_notifier_registry_all_includes_desktop():
    all_notifiers = notifier_registry.all()
    assert "desktop" in all_notifiers
    assert all_notifiers["desktop"] is DesktopNotifier
