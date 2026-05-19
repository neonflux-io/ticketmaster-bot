"""Multi-account orchestration primitives.

This subpackage owns the parallel coordinator that races one
:class:`~src.vendors.ticketmaster.core.BotRunner` per account, plus the
:class:`~src.orchestrator.lifecycle.LifecycleDispatcher` that fans
runner-level events out to every registered
:class:`~src.hooks.base.Hook`.
"""

from __future__ import annotations

from .lifecycle import LIFECYCLE_EVENTS, LifecycleDispatcher
from .parallel import ParallelCoordinator, default_runner_factory

__all__ = [
    "LIFECYCLE_EVENTS",
    "LifecycleDispatcher",
    "ParallelCoordinator",
    "default_runner_factory",
]
