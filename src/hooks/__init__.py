"""Lifecycle hook framework.

Public surface:

* :class:`~src.hooks.base.Hook` — ABC every concrete hook subclasses.
* :class:`~src.hooks.base.HookRegistry` — per-runner ordered collection of
  registered hook instances. Distinct from the module-level
  :mod:`src.registry.hooks` singleton, which stores hook *classes* for
  entry-point auto-discovery.

The orchestrator side of this framework lives in
:mod:`src.orchestrator.lifecycle`.
"""

from __future__ import annotations

from .base import Hook, HookRegistry

__all__ = ["Hook", "HookRegistry"]
