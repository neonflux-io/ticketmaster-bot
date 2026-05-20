"""Ticketmaster Singapore vendor adapter package.

Importing this package has a side effect: it registers
:class:`TicketmasterSGAdapter` with :mod:`src.registry.vendors` under
the name ``"ticketmaster_sg"``. The registration is idempotent so a
second import (or a registry reload during testing) is a no-op.

Callers can do::

    from src.registry import vendors
    Adapter = vendors.get("ticketmaster_sg")
    runner = Adapter().build_runner(config, account=acct)
"""

from __future__ import annotations

from src.registry import vendors as _vendor_registry

from . import auth, cart, checkout, core, navigator, price, queue, seatmap
from .adapter import TicketmasterSGAdapter
from .seatmap import SGInteractiveSeatmapStrategy
from .selectors import locator, locator_multi, selector_for

# Idempotent registration so re-import during test reloads does not raise.
if "ticketmaster_sg" not in _vendor_registry.registry:
    _vendor_registry.register("ticketmaster_sg", TicketmasterSGAdapter)

__all__ = [
    "SGInteractiveSeatmapStrategy",
    "TicketmasterSGAdapter",
    "auth",
    "cart",
    "checkout",
    "core",
    "locator",
    "locator_multi",
    "navigator",
    "price",
    "queue",
    "seatmap",
    "selector_for",
]
