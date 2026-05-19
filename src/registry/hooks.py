"""Singleton registry for lifecycle hooks."""
from __future__ import annotations

from typing import Any

from .base import Registry

registry: Registry[Any] = Registry(
    "hooks", entry_point_group="ticketmaster_bot.hooks"
)


def register(name: str, obj: Any) -> Any:
    """Register a hook class or factory under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> Any:
    """Look up a hook by name."""
    return registry.get(name)


def all() -> dict[str, Any]:
    """Return a snapshot of all registered hooks."""
    return registry.all()


__all__ = ["all", "get", "register", "registry"]
