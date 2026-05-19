"""Hook ABC and the per-runner :class:`HookRegistry`.

A :class:`Hook` observes lifecycle events fired by a
:class:`~src.orchestrator.lifecycle.LifecycleDispatcher`. The ABC keeps the
contract minimal:

* ``enabled`` — gate flag; the dispatcher skips disabled hooks.
* ``priority`` — integer ordering key (lower fires first). Hooks at the
  same priority fire in registration order.
* ``async on_event(ctx, event_type, **kwargs)`` — the single coroutine
  every concrete subclass must override.

The :class:`HookRegistry` is a per-runner container of registered hook
*instances* (distinct from the module-level
:mod:`src.registry.hooks` singleton which stores hook *classes* for
entry-point discovery). It iterates hooks in priority order so the
:class:`~src.orchestrator.lifecycle.LifecycleDispatcher` can fire them
predictably.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any


class Hook(ABC):
    """Abstract base class for lifecycle hooks.

    Subclasses override :meth:`on_event` and may override the class-level
    :attr:`enabled` / :attr:`priority` attributes (or set them on the
    instance) to control whether and when they run.

    The default ``priority`` of ``0`` means "ordinary"; built-in hooks
    that need to run before observers (e.g. a metrics-incrementing hook
    that snapshots state) use a negative priority, and built-in hooks
    that run as final cleanup (e.g. log dump on failure) use a positive
    priority.
    """

    #: Default class-level enable flag. Subclasses or instances may
    #: override this; the dispatcher honours the instance-level value.
    enabled: bool = True

    #: Default class-level priority (lower fires first; ties keep
    #: registration order).
    priority: int = 0

    @abstractmethod
    async def on_event(self, ctx: Any, event_type: str, **kwargs: Any) -> None:
        """Handle one lifecycle event.

        Parameters
        ----------
        ctx:
            Caller-supplied context. The production
            :class:`~src.vendors.ticketmaster.core.BotRunner` passes
            ``self`` so hooks can read the configuration and account
            without juggling extra arguments. Tests pass arbitrary
            objects (often the recording dict that captures fired
            events).
        event_type:
            One of the names in
            :data:`src.orchestrator.lifecycle.LIFECYCLE_EVENTS`.
        kwargs:
            Per-event payload. Each call site documents the keys it
            supplies; hooks must tolerate missing keys (use
            ``kwargs.get(...)``) because runners may grow new payloads
            over time without bumping every hook implementation.
        """


class HookRegistry:
    """Ordered collection of :class:`Hook` instances.

    Hooks are stored in registration order. :meth:`all` and
    :meth:`__iter__` yield them sorted by :attr:`Hook.priority` (lower
    first) with registration order as the stable tie-breaker. Hooks with
    :attr:`Hook.enabled` set to ``False`` still appear in the iteration
    so callers can introspect the full set; the dispatcher applies the
    ``enabled`` filter itself.
    """

    def __init__(self) -> None:
        self._hooks: list[Hook] = []

    def register(self, hook: Hook) -> Hook:
        """Append ``hook`` to the registry.

        Returns ``hook`` unchanged so this method can be used as a
        single-line registration helper.
        """
        if not isinstance(hook, Hook):
            raise TypeError(
                f"HookRegistry.register expected a Hook instance, got {type(hook).__name__}"
            )
        self._hooks.append(hook)
        return hook

    def register_many(self, hooks: Iterable[Hook]) -> None:
        """Register every hook in ``hooks`` in order."""
        for hook in hooks:
            self.register(hook)

    def unregister(self, hook: Hook) -> None:
        """Remove ``hook`` from the registry. No-op if not present."""
        try:
            self._hooks.remove(hook)
        except ValueError:
            pass

    def clear(self) -> None:
        """Drop every registered hook."""
        self._hooks.clear()

    def all(self) -> list[Hook]:
        """Return hooks ordered by priority ascending (stable on ties)."""
        # Python's sort is stable, so equal-priority hooks keep their
        # registration order.
        return sorted(self._hooks, key=lambda h: h.priority)

    def __iter__(self) -> Iterator[Hook]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self._hooks)

    def __contains__(self, hook: object) -> bool:
        return hook in self._hooks


__all__ = ["Hook", "HookRegistry"]
