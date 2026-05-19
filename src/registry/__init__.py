"""Typed plugin registries for ticketmaster-bot.

Each submodule (``strategies``, ``vendors``, ``notifiers``, ``hooks``,
``selectors``) defines a singleton :class:`~src.registry.base.Registry`
instance plus convenience ``register``/``get``/``all`` module-level helpers
that delegate to the singleton.
"""

from __future__ import annotations

from . import hooks, notifiers, selectors, strategies, vendors
from .base import (
    DuplicateRegistration,
    NotRegistered,
    Registry,
    RegistryError,
)

__all__ = [
    "DuplicateRegistration",
    "NotRegistered",
    "Registry",
    "RegistryError",
    "hooks",
    "notifiers",
    "selectors",
    "strategies",
    "vendors",
]
