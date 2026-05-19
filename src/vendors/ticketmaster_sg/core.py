"""ticketmaster.sg state-machine runner.

The SG flow has the same shape as the US flow (login → navigate →
on-sale wait → queue → ticket select → cart → checkout) so we
subclass the existing :class:`src.vendors.ticketmaster.core.BotRunner`
and inject the SG-specific per-step modules. No behaviour from the
state machine itself needs to change: the parent runner reads its
step modules from instance attributes that this subclass binds to
the SG implementations at construction time.

Keeping the state machine in one place ensures every lifecycle hook
the US flow already fires (``on_run_start``, ``before_login``,
``after_queue_release``, ``before_cart`` … ``on_run_end``) fires
identically for the SG flow, with zero duplication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..ticketmaster.core import BotRunner as _BaseBotRunner
from . import auth, cart, checkout, navigator, queue

if TYPE_CHECKING:
    from ...utils.config_loader import AccountConfig, BotConfig


class BotRunner(_BaseBotRunner):
    """SG-bound :class:`BotRunner`. Inherits the state machine, swaps the steps."""

    # Class-level module bindings consulted by the parent ``__init__``.
    # Constructor kwargs still win so tests can inject ad-hoc steps.
    auth_module = auth
    cart_module = cart
    checkout_module = checkout
    navigator_module = navigator
    queue_module = queue

    def __init__(self, config: BotConfig, account: AccountConfig | None = None) -> None:
        super().__init__(config, account=account)


__all__ = ["BotRunner"]
