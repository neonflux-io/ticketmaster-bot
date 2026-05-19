"""Ticketmaster Singapore vendor adapter package.

Imports the per-step modules so callers can do
``from src.vendors.ticketmaster_sg import auth, cart, checkout, navigator, queue``.

This package does *not* register a :class:`VendorAdapter` yet — that is
the responsibility of F7.5, which adds :mod:`adapter` plus a
:func:`src.registry.vendors.register` call here. F7.3 + F7.4 ship the
per-step modules (auth / navigator / queue / cart / checkout) so
subsequent features can build on them.
"""

from __future__ import annotations

from . import auth, cart, checkout, navigator, price, queue
from .selectors import locator, locator_multi, selector_for

__all__ = [
    "auth",
    "cart",
    "checkout",
    "locator",
    "locator_multi",
    "navigator",
    "price",
    "queue",
    "selector_for",
]
