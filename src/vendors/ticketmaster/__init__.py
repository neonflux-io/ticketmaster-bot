"""Ticketmaster vendor adapter package.

Importing this package has a side effect: it registers
:class:`TicketmasterAdapter` with :mod:`src.registry.vendors` under the
name ``"ticketmaster"`` so callers can do::

    from src.registry import vendors
    Adapter = vendors.get("ticketmaster")
"""

from __future__ import annotations

from src.registry import vendors as _vendor_registry

from . import auth, cart, checkout, core, navigator, queue
from .adapter import TicketmasterAdapter

# Idempotent registration: the registry raises DuplicateRegistration on a
# second register() with the same name, which would break test reloads.
if "ticketmaster" not in _vendor_registry.registry:
    _vendor_registry.register("ticketmaster", TicketmasterAdapter)

__all__ = [
    "TicketmasterAdapter",
    "auth",
    "cart",
    "checkout",
    "core",
    "navigator",
    "queue",
]
