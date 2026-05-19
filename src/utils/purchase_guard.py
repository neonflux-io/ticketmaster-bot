"""Global kill switch for real ticket purchases.

This module enforces the mission-wide invariant that no real purchase can be
completed. Even if config.checkout.auto_purchase=True, the place-order click
is blocked by PurchaseGuard.gate().

To actually purchase tickets, a user must explicitly set the environment
variable TICKETMASTER_BOT_PURCHASE_ALLOWED=I_UNDERSTAND_REAL_MONEY (long
enough that it can't be set by accident or autocomplete).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("ticketmaster-bot")

PURCHASE_OVERRIDE_ENV = "TICKETMASTER_BOT_PURCHASE_ALLOWED"
PURCHASE_OVERRIDE_VALUE = "I_UNDERSTAND_REAL_MONEY"


class PurchaseBlocked(Exception):
    """Raised when a purchase-completion attempt is blocked by the guard."""


def purchase_allowed() -> bool:
    """Return True only when the env override is set to the exact magic value."""
    return os.environ.get(PURCHASE_OVERRIDE_ENV) == PURCHASE_OVERRIDE_VALUE


def gate(action_name: str = "place_order") -> None:
    """Raise PurchaseBlocked unless the env override is set.

    Call this immediately before any code path that would commit a real purchase.
    """
    if not purchase_allowed():
        log.error(
            "[purchase-guard] BLOCKED %s — set %s=%s to allow.",
            action_name,
            PURCHASE_OVERRIDE_ENV,
            PURCHASE_OVERRIDE_VALUE,
        )
        raise PurchaseBlocked(
            f"Refusing to perform {action_name!r}: real-money purchase guard active. "
            f"Set {PURCHASE_OVERRIDE_ENV}={PURCHASE_OVERRIDE_VALUE} to override "
            "(NOT recommended)."
        )
    log.warning(
        "[purchase-guard] ALLOWING %s because %s=%s is set. REAL MONEY WILL BE SPENT.",
        action_name,
        PURCHASE_OVERRIDE_ENV,
        PURCHASE_OVERRIDE_VALUE,
    )


__all__ = [
    "PURCHASE_OVERRIDE_ENV",
    "PURCHASE_OVERRIDE_VALUE",
    "PurchaseBlocked",
    "gate",
    "purchase_allowed",
]
