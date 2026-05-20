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

The SG runner additionally overrides two extension points the base
runner exposes specifically for vendor-specific picker UIs:

* :meth:`_build_strategy` — when the operator selects
  ``tickets.strategy=interactive_seatmap``, substitutes the
  SG-tailored :class:`SGInteractiveSeatmapStrategy` (which clicks
  ``<td>`` cells inside the SG Fancybox ``<table class='seat'>``) for
  the shared :class:`InteractiveSeatmapStrategy` (which clicks
  ``<rect>`` elements in the US React app's SVG seat-map).
* :meth:`_select_ticket` — when the active strategy is the SG seat-
  map variant, opens the Fancybox iframe by clicking
  ``button#manualMode`` on the ticket-area page, runs the SG
  strategy's pick against the resulting iframe, then clicks
  ``button#submitSeat`` to confirm. The page subsequently
  transitions to ``/ticket/check-captcha/<...>`` and the rest of the
  state machine (cart → checkout) handles it identically to the
  area-only flow.

See ``docs/vendors_ticketmaster_sg.md`` (``Seat-map events``) for the
operator-facing configuration of this branch.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..ticketmaster.core import BotRunner as _BaseBotRunner
from . import auth, cart, checkout, navigator, queue
from . import selectors as sg_selectors
from .seatmap import SGInteractiveSeatmapStrategy

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...strategies.base import SelectionStrategy, TicketCandidate
    from ...utils.config_loader import AccountConfig, BotConfig

log = logging.getLogger("ticketmaster-bot")


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

    # -----------------------------------------------------------------
    # Seat-map extension points
    # -----------------------------------------------------------------

    def _build_strategy(self) -> SelectionStrategy:
        """Return the SG seat-map strategy when configured; else delegate.

        The shared strategy factory builds
        :class:`src.strategies.interactive_seatmap.InteractiveSeatmapStrategy`
        for ``tickets.strategy=interactive_seatmap``, but that strategy
        targets ``<rect>`` elements inside the US React seat-map SVG.
        The SG seat-map renders as a ``<table class='seat'>`` with
        ``<td data-seatrow data-seatno>`` cells inside a Fancybox
        iframe, so the SG runner substitutes
        :class:`SGInteractiveSeatmapStrategy` instead.
        """
        cfg = self.config.tickets
        if cfg.strategy == "interactive_seatmap":
            seatmap = cfg.interactive_seatmap
            # The config loader enforces non-empty section/row/seat
            # when strategy=interactive_seatmap, so the casts are safe.
            return SGInteractiveSeatmapStrategy(
                section=str(seatmap.section),
                row=str(seatmap.row),
                seat=str(seatmap.seat),
            )
        return super()._build_strategy()

    async def _select_ticket(
        self,
        page: Page,
        strategy: SelectionStrategy,
    ) -> TicketCandidate | None:
        """Open the SG seat-map iframe before the SG seat-map strategy runs.

        When the operator picks ``tickets.strategy=interactive_seatmap``
        the SG flow needs an extra DOM step before the strategy can
        find its target cell:

        1. Click ``button#manualMode`` on the ticket-area page (the
           "Pick Your Own Seat" trigger). This opens the Fancybox
           ``<iframe src='/ticket/select-seat/...'>``.
        2. Wait for the seat-map iframe to attach to ``page.frames``.
        3. Run :meth:`SGInteractiveSeatmapStrategy.pick`, which
           descends into the iframe and clicks the target cell.
        4. Click ``button#submitSeat`` inside the iframe to confirm
           the selection. The SG site then closes the Fancybox and
           submits the parent ``<form>`` so the URL transitions to
           ``/ticket/check-captcha/<...>``.

        For any non-seat-map strategy the base runner's behaviour is
        preserved (the strategy's ``pick`` runs unchanged against the
        ticket-area page).
        """
        if not isinstance(strategy, SGInteractiveSeatmapStrategy):
            return await super()._select_ticket(page, strategy)

        log.info("SG seat-map flow: opening Fancybox via #manualMode")
        try:
            manual_btn = sg_selectors.locator(page, "ticket_form_manual_seat_button")
            await manual_btn.wait_for(state="visible", timeout=8000)
            await manual_btn.click()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SG seat-map flow: could not click 'Pick Your Own Seat' "
                "(#manualMode) — area page may not expose a seat-map for "
                "this section. Falling back to None. (%s)",
                exc,
            )
            return None

        try:
            iframe_sel = sg_selectors.selector_for("seatmap_iframe")
            await page.wait_for_selector(iframe_sel, timeout=10000)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SG seat-map flow: seat-map iframe never attached after clicking #manualMode (%s)",
                exc,
            )
            return None

        candidate = await strategy.pick(page)
        if candidate is None:
            log.warning(
                "SG seat-map strategy could not find the requested cell. "
                "Operator should consider falling back to "
                "tickets.strategy=best_available for this event."
            )
            return None

        log.info("SG seat-map flow: clicking #submitSeat to confirm selection")
        try:
            submit_btn = sg_selectors.locator(page, "seatmap_submit_button")
            iframe_locator = page.frame_locator(iframe_sel)
            # The submit button lives inside the iframe. Try the
            # iframe-scoped locator first; fall back to the top-level
            # page for the standalone-fixture case where the seat-map
            # HTML is loaded directly (no iframe wrapper).
            try:
                in_frame_submit = iframe_locator.locator(
                    sg_selectors.selector_for("seatmap_submit_button")
                ).first
                if await in_frame_submit.count() > 0:
                    await in_frame_submit.click(timeout=5000)
                else:
                    await submit_btn.click(timeout=5000)
            except Exception:  # noqa: BLE001
                await submit_btn.click(timeout=5000)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SG seat-map flow: could not click 'Confirm Seats' (#submitSeat) "
                "after selecting cell. The cell was clicked but the form was "
                "never submitted. (%s)",
                exc,
            )
            return None

        # Give the inline AJAX submit + Fancybox-close + parent-form
        # submit time to navigate to /ticket/check-captcha/<...>. We
        # don't fail hard if this times out — downstream cart.add_to_cart
        # and the captcha-wait helper can both cope with a slightly
        # delayed URL transition.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass

        return candidate


__all__ = ["BotRunner"]
