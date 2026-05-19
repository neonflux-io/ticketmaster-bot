"""Vendor adapters for ticket-purchasing flows.

Each subpackage under :mod:`src.vendors` implements a concrete
:class:`~src.vendors.base.VendorAdapter`. The Ticketmaster adapter is the
only one that ships today and lives in :mod:`src.vendors.ticketmaster`.

Importing this package registers every shipped adapter into
:mod:`src.registry.vendors` so callers can do::

    from src.registry import vendors
    Adapter = vendors.get("ticketmaster")
    runner = Adapter().build_runner(config)
"""

from __future__ import annotations

from . import ticketmaster  # noqa: F401  (registers TicketmasterAdapter)
from .base import VendorAdapter

__all__ = ["VendorAdapter", "ticketmaster"]
