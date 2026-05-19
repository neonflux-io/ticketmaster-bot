"""Legacy ``src.bot`` package - thin re-export shim over ``src.vendors.ticketmaster``.

The Ticketmaster vendor flow lives under :mod:`src.vendors.ticketmaster`
after the F1.2 refactor. ``src.bot`` is preserved as a thin backwards-compat
shim so existing code that imports ``from src.bot import auth`` (or any of
the other per-step modules) keeps working.

This shim will be removed in a future release; new code should import from
:mod:`src.vendors.ticketmaster` directly.
"""

from __future__ import annotations

from src.vendors.ticketmaster import (
    auth,
    cart,
    checkout,
    core,
    navigator,
    queue,
)

__all__ = ["auth", "cart", "checkout", "core", "navigator", "queue"]
