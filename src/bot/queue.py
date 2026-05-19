"""Legacy shim - real implementation lives in src.vendors.ticketmaster.queue."""

from __future__ import annotations

from src.vendors.ticketmaster.queue import *  # noqa: F401,F403
from src.vendors.ticketmaster.queue import (  # noqa: F401
    QueueTimeoutError,
    in_queue,
    wait_through_queue,
)
