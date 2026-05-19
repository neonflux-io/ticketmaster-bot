"""Legacy shim - real implementation lives in src.vendors.ticketmaster.cart."""

from __future__ import annotations

from src.vendors.ticketmaster.cart import *  # noqa: F401,F403
from src.vendors.ticketmaster.cart import (  # noqa: F401
    CartError,
    accept_terms_if_needed,
    add_to_cart,
    set_quantity,
)
