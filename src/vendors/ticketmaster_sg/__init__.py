"""Ticketmaster Singapore vendor adapter package.

Imports the per-step modules so callers can do
``from src.vendors.ticketmaster_sg import auth, navigator, queue``.

This package does *not* register a :class:`VendorAdapter` yet — that is
the responsibility of F7.5, which adds :mod:`adapter` plus a
:func:`src.registry.vendors.register` call here. F7.3 only ships the
per-step modules (auth / navigator / queue) so subsequent features can
build on them.
"""

from __future__ import annotations

from . import auth, navigator, queue
from .selectors import locator, locator_multi, selector_for

__all__ = [
    "auth",
    "locator",
    "locator_multi",
    "navigator",
    "queue",
    "selector_for",
]
