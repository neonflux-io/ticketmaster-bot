"""Notifier subsystem.

Exposes the :class:`NotifyEvent` payload, the abstract :class:`Notifier`
base class, and the shipped concrete notifiers. Importing this package
also registers the shipped notifiers in
:mod:`src.registry.notifiers` so they are reachable via the
:class:`NotifierRegistry` lookup helpers.
"""

from __future__ import annotations

from ..registry import notifiers as _notifier_registry
from .base import Notifier, NotifierError, NotifyEvent
from .desktop import DesktopNotifier
from .discord import DiscordNotifier
from .telegram import TelegramNotifier
from .webhook import WebhookNotifier


def _register_default_notifiers() -> None:
    """Register every shipped notifier class under its config key.

    Re-imports are tolerated by skipping any name already present in the
    registry (the registry itself raises ``DuplicateRegistration`` on
    duplicate keys).
    """
    defaults: dict[str, type[Notifier]] = {
        "desktop": DesktopNotifier,
        "webhook": WebhookNotifier,
        "discord": DiscordNotifier,
        "telegram": TelegramNotifier,
    }
    for name, cls in defaults.items():
        if name in _notifier_registry.registry:
            continue
        _notifier_registry.register(name, cls)


_register_default_notifiers()


__all__ = [
    "DesktopNotifier",
    "DiscordNotifier",
    "Notifier",
    "NotifierError",
    "NotifyEvent",
    "TelegramNotifier",
    "WebhookNotifier",
]
