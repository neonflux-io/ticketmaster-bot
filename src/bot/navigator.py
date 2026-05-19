"""Legacy shim - real implementation lives in src.vendors.ticketmaster.navigator."""

from __future__ import annotations

from src.vendors.ticketmaster.navigator import *  # noqa: F401,F403
from src.vendors.ticketmaster.navigator import (  # noqa: F401
    NavigationError,
    detect_state,
    open_event,
    wait_until_on_sale,
)
