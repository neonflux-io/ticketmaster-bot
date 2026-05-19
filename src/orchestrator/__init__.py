"""Multi-account orchestration primitives.

This subpackage owns the parallel coordinator that races one
:class:`~src.vendors.ticketmaster.core.BotRunner` per account.

Today only :class:`~src.orchestrator.parallel.ParallelCoordinator` is
exposed. Future additions (sequential runner pools, retry queues,
priority orderings) will land alongside it.
"""

from __future__ import annotations

from .parallel import ParallelCoordinator, default_runner_factory

__all__ = ["ParallelCoordinator", "default_runner_factory"]
