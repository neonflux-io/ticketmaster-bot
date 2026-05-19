"""Singleton registry for vendor adapters (Ticketmaster, AXS, ...)."""

from __future__ import annotations

from typing import Any

from .base import Registry

registry: Registry[Any] = Registry("vendors", entry_point_group="ticketmaster_bot.vendors")


def register(name: str, obj: Any) -> Any:
    """Register a vendor adapter class under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> Any:
    """Look up a vendor adapter by name."""
    return registry.get(name)


def all() -> dict[str, Any]:
    """Return a snapshot of all registered vendor adapters."""
    return registry.all()


__all__ = ["all", "get", "register", "registry"]
