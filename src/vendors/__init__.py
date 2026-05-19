"""Vendor adapters for ticket-purchasing flows.

Each subpackage under :mod:`src.vendors` implements a concrete
:class:`~src.vendors.base.VendorAdapter`. Two adapters ship today:
the US/CA Ticketmaster adapter (:mod:`src.vendors.ticketmaster`) and
the Singapore variant (:mod:`src.vendors.ticketmaster_sg`).

Importing this package registers every shipped adapter into
:mod:`src.registry.vendors` so callers can do::

    from src.registry import vendors
    Adapter = vendors.get("ticketmaster")        # US/CA
    Adapter = vendors.get("ticketmaster_sg")    # Singapore
    runner = Adapter().build_runner(config)
"""

from __future__ import annotations

from . import (
    ticketmaster,  # noqa: F401  (registers TicketmasterAdapter)
    ticketmaster_sg,  # noqa: F401  (registers TicketmasterSGAdapter)
)
from .base import VendorAdapter

__all__ = ["VendorAdapter", "ticketmaster", "ticketmaster_sg"]
