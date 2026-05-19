"""Legacy shim - real implementation lives in src.vendors.ticketmaster.checkout."""

from __future__ import annotations

from src.vendors.ticketmaster.checkout import *  # noqa: F401,F403
from src.vendors.ticketmaster.checkout import (  # noqa: F401
    CheckoutError,
    run_checkout,
    select_delivery,
    select_saved_card,
    verify_cart_matches,
)
