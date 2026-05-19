"""Concrete :class:`VendorAdapter` for ticketmaster.sg.

Mirrors :class:`src.vendors.ticketmaster.adapter.TicketmasterAdapter` —
exposes the SG-specific per-step modules and a :meth:`build_runner`
factory that returns the SG-bound
:class:`src.vendors.ticketmaster_sg.core.BotRunner`. The SG runner
inherits its state machine from the US runner and only swaps the
underlying per-step modules, so the lifecycle dispatcher / hook
registry / proxy / stealth wiring stays identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import VendorAdapter
from . import auth, cart, checkout, core, navigator, queue

if TYPE_CHECKING:
    from ...utils.config_loader import AccountConfig, BotConfig


class TicketmasterSGAdapter(VendorAdapter):
    """:class:`VendorAdapter` implementation that wires the SG modules in.

    Each instance exposes the per-step modules as attributes so callers
    can write ``adapter.auth.login(...)`` /
    ``adapter.cart.add_to_cart(...)`` without importing the SG
    submodules directly.
    """

    name = "ticketmaster_sg"

    def __init__(self) -> None:
        # Module attributes required by the ABC.
        self.auth = auth
        self.cart = cart
        self.checkout = checkout
        self.core = core
        self.navigator = navigator
        self.queue = queue

    def build_runner(
        self, config: BotConfig, account: AccountConfig | None = None
    ) -> core.BotRunner:
        """Construct an SG-bound :class:`BotRunner` for ``config`` + ``account``."""
        return core.BotRunner(config, account=account)


__all__ = ["TicketmasterSGAdapter"]
