"""Singleton registry for logical-name → selector-list mappings.

Selector YAMLs (``config/selectors/*.yaml``) are loaded into this registry
by later mission features (F1.3). For now this module exposes the same
``register`` / ``get`` / ``all`` surface as the other registries so the
plugin-loading infrastructure is uniform across kinds.
"""
from __future__ import annotations

from typing import Any

from .base import Registry

registry: Registry[Any] = Registry(
    "selectors", entry_point_group="ticketmaster_bot.selectors"
)


def register(name: str, obj: Any) -> Any:
    """Register a selector list (or selector provider) under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> Any:
    """Look up a selector entry by logical name."""
    return registry.get(name)


def all() -> dict[str, Any]:
    """Return a snapshot of all registered selector entries."""
    return registry.all()


__all__ = ["all", "get", "register", "registry"]
