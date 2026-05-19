"""Abstract base class for ticket-purchasing vendor adapters.

A :class:`VendorAdapter` bundles together the per-step modules
(authentication, navigation, queue handling, cart, checkout) and the
end-to-end runner for a single ticket vendor. Today only the Ticketmaster
adapter is shipped; future adapters (AXS, SeeTickets, ...) are expected to
subclass :class:`VendorAdapter` and register themselves with
:mod:`src.registry.vendors`.

The ABC deliberately exposes the *modules* used by the runner rather than
re-defining every step on the adapter itself, so the existing
``BotRunner`` state machine in :mod:`src.vendors.ticketmaster.core` keeps
its current shape. Subclasses fill in the ``auth``, ``navigator``,
``queue``, ``cart``, ``checkout`` and ``core`` attributes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.utils.config_loader import AccountConfig, BotConfig


class VendorAdapter(ABC):
    """Abstract adapter that owns a vendor's per-step modules + runner factory.

    Concrete subclasses must set the ``auth``, ``navigator``, ``queue``,
    ``cart``, ``checkout`` and ``core`` attributes on each instance to the
    module objects implementing the per-step flow. The :meth:`build_runner`
    factory should return a runner object whose ``run()`` coroutine drives
    the full purchase flow for one account.
    """

    #: Stable, lowercase identifier used by :mod:`src.registry.vendors`.
    name: str = ""

    #: Per-step modules. Subclasses bind these in ``__init__``.
    auth: ModuleType
    navigator: ModuleType
    queue: ModuleType
    cart: ModuleType
    checkout: ModuleType
    core: ModuleType

    @abstractmethod
    def build_runner(self, config: BotConfig, account: AccountConfig | None = None) -> Any:
        """Return a runner object capable of executing the vendor's flow.

        The returned object must expose an ``async run()`` method returning
        a truthy value on success.
        """

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"{type(self).__name__}(name={self.name!r})"


__all__ = ["VendorAdapter"]
