"""Singleton registry for captcha solvers.

Follows the same ``Registry[T]`` pattern as
:mod:`src.registry.strategies` / :mod:`src.registry.notifiers`: a
module-level :class:`~src.registry.base.Registry` instance plus convenience
``register`` / ``get`` / ``all`` helpers that delegate to it.

The :class:`CaptchaSolverRegistry` alias is exported so callers (notably
:mod:`src.captcha.chain`) can construct their own isolated registry in
tests without dragging the singleton around.
"""

from __future__ import annotations

from typing import Any

from ..registry.base import Registry

#: Type alias for an isolated captcha solver registry. ``Registry[T]`` is
#: already generic, but exposing it under a captcha-specific name keeps the
#: public API of this module discoverable from one place.
CaptchaSolverRegistry = Registry

registry: Registry[Any] = Registry(
    "captcha",
    entry_point_group="ticketmaster_bot.captcha",
)


def register(name: str, obj: Any) -> Any:
    """Register a captcha solver class or instance under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> Any:
    """Look up a captcha solver by name."""
    return registry.get(name)


def all() -> dict[str, Any]:
    """Return a snapshot of all registered captcha solvers."""
    return registry.all()


__all__ = [
    "CaptchaSolverRegistry",
    "all",
    "get",
    "register",
    "registry",
]
