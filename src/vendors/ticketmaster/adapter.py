"""Concrete :class:`VendorAdapter` for Ticketmaster.

The adapter is intentionally thin: it bundles the moved per-step modules
together with the existing :class:`~src.vendors.ticketmaster.core.BotRunner`
and exposes them through the :class:`~src.vendors.base.VendorAdapter`
interface. The Ticketmaster flow logic itself has not changed - the moved
modules are imported verbatim and re-exposed on the adapter instance so
flow code can keep using e.g. ``adapter.auth.login(...)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import VendorAdapter
from . import auth, cart, checkout, core, navigator, queue

if TYPE_CHECKING:
    from ...utils.config_loader import AccountConfig, BotConfig


class TicketmasterAdapter(VendorAdapter):
    """:class:`VendorAdapter` implementation that wires the moved modules in.

    Each instance exposes the per-step modules as attributes so callers can
    write ``adapter.auth.login(...)`` or ``adapter.cart.add_to_cart(...)``
    without importing :mod:`src.vendors.ticketmaster.auth` directly.
    """

    name = "ticketmaster"

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
        """Construct a :class:`BotRunner` bound to the given config + account."""
        return core.BotRunner(config, account=account)


__all__ = ["TicketmasterAdapter"]
