"""Singleton registry for notifier channels (Discord, Slack, Webhook, ...)."""

from __future__ import annotations

from typing import Any

from .base import Registry

registry: Registry[Any] = Registry("notifiers", entry_point_group="ticketmaster_bot.notifiers")


def register(name: str, obj: Any) -> Any:
    """Register a notifier class or factory under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> Any:
    """Look up a notifier by name."""
    return registry.get(name)


def all() -> dict[str, Any]:
    """Return a snapshot of all registered notifiers."""
    return registry.all()


__all__ = ["all", "get", "register", "registry"]
