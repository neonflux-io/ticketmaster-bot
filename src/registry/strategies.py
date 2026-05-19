"""Singleton registry for ticket-selection strategies."""
from __future__ import annotations

from typing import Any

from .base import Registry

registry: Registry[Any] = Registry(
    "strategies", entry_point_group="ticketmaster_bot.strategies"
)


def register(name: str, obj: Any) -> Any:
    """Register a strategy class or factory under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> Any:
    """Look up a strategy by name."""
    return registry.get(name)


def all() -> dict[str, Any]:
    """Return a snapshot of all registered strategies."""
    return registry.all()


__all__ = ["all", "get", "register", "registry"]
