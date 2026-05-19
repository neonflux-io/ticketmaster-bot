"""Core bot orchestrator - the state machine that drives everything."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import async_playwright

from ...strategies.factory import build_strategy
from ...utils.notifier import notify
from ...utils.retry import maybe_human_delay
from ...utils.stealth import (
    apply_stealth,
    jittered_viewport,
    random_ua,
)
from . import auth, cart, checkout, navigator, queue

if TYPE_CHECKING:
    from ...utils.config_loader import AccountConfig, BotConfig

log = logging.getLogger("ticketmaster-bot")


class BotRunner:
    """Orchestrates the entire ticket-buying flow for one account."""

    def __init__(self, config: BotConfig, account: AccountConfig | None = None) -> None:
        self.config = config
        self.account = account or (config.accounts[0] if config.accounts else None)
        delays = config.timing.action_delay_seconds
        self.action_delay_min = float(delays[0])
        self.action_delay_max = float(delays[1])
        self.humanize = bool(config.timing.humanize)

    async def _delay(self) -> None:
        await maybe_human_delay(self.humanize, self.action_delay_min, self.action_delay_max)

    async def run(self) -> bool:
        """Run the bot end-to-end. Returns True on success."""
        cfg = self.config
        user_data_dir = Path(cfg.browser.user_data_dir)
        if self.account:
            user_data_dir = user_data_dir.parent / f"{user_data_dir.name}-{self.account.name}"
        user_data_dir.mkdir(parents=True, exist_ok=True)

        stealth_cfg = cfg.browser.stealth
        user_agent = (
            stealth_cfg.user_agent
            if stealth_cfg.user_agent
            else (random_ua() if stealth_cfg.enabled else None)
        )
        viewport = (
            jittered_viewport(jitter=stealth_cfg.viewport_jitter)
            if stealth_cfg.enabled
            else {"width": 1366, "height": 900}
        )

        async with async_playwright() as p:
            log.info("Launching browser (headless=%s)", cfg.browser.headless)
            # --disable-blink-features=AutomationControlled removes one
            # automation tell. apply_stealth() handles the others.
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=cfg.browser.headless,
                slow_mo=cfg.browser.slow_mo_ms,
                locale=cfg.browser.locale,
                timezone_id=cfg.browser.timezone,
                viewport=viewport,
                user_agent=user_agent,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--disable-features=AutomationControlled",
                ],
            )
            await apply_stealth(context, stealth_cfg)
            try:
                return await asyncio.wait_for(
                    self._run_flow(context),
                    timeout=cfg.timing.max_total_runtime_seconds,
                )
            except asyncio.TimeoutError:
                log.error("Bot exceeded max_total_runtime_seconds - aborting")
                return False
            finally:
                if not cfg.browser.headless and cfg.timing.hold_open_seconds > 0:
                    log.info(
                        "Leaving browser open for %.0fs so you can inspect/checkout",
                        cfg.timing.hold_open_seconds,
                    )
                    await _sleep_until_browser_closed(context, cfg.timing.hold_open_seconds)
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _run_flow(self, context) -> bool:  # noqa: ANN001 - playwright type
        cfg = self.config

        # 1. Ensure we're logged in
        if self.account:
            logged_in = await auth.is_logged_in(context)
            if not logged_in:
                log.info("Not logged in - performing login")
                humanize_cfg = cfg.timing.humanize
                typing_cfg = (
                    humanize_cfg.typing
                    if humanize_cfg.enabled and humanize_cfg.typing.enabled
                    else None
                )
                await auth.login(
                    context,
                    self.account,
                    action_delay=(self.action_delay_min, self.action_delay_max),
                    typing_cfg=typing_cfg,
                )
            else:
                log.info("Existing session detected for %s", self.account.name)
        else:
            log.warning(
                "No account configured - assuming session persistence handles auth. "
                "Run with browser visible and log in manually if needed."
            )

        page = await context.new_page()

        # 2. Navigate to event page
        await navigator.open_event(
            page, cfg.event.url, timeout_seconds=cfg.timing.page_timeout_seconds
        )

        # 3. Wait for on-sale (no-op if already on sale)
        if cfg.event.on_sale_time is not None:
            await navigator.wait_until_on_sale(
                page,
                cfg.event.on_sale_time,
                refresh_interval_seconds=cfg.event.refresh_interval_seconds,
                poll_deadline_seconds=cfg.timing.max_total_runtime_seconds,
            )

        # 4. Handle queue if present (do NOT reload after release - we'd lose
        #    the queue token and get dumped back into the waiting room)
        state = await navigator.detect_state(page)
        log.info("Initial page state: %s", state)
        if state == "queue":
            await queue.wait_through_queue(
                page,
                check_interval_seconds=cfg.timing.queue_check_interval_seconds,
                max_wait_seconds=cfg.timing.max_total_runtime_seconds,
            )
            state = await navigator.detect_state(page)
            log.info("Post-queue page state: %s", state)

        if state == "sold_out":
            log.error("Event is sold out - nothing to do")
            notify(
                "Ticketmaster Bot",
                "Event is sold out",
                desktop=cfg.notifications.desktop,
                sound=cfg.notifications.sound,
            )
            return False
        if state == "not_on_sale":
            log.error("Event is not on sale yet")
            notify(
                "Ticketmaster Bot",
                "Event is not on sale yet",
                desktop=cfg.notifications.desktop,
                sound=cfg.notifications.sound,
            )
            return False

        # 5. Select tickets
        strategy = build_strategy(cfg.tickets)
        await self._delay()
        chosen = await strategy.pick(page)
        if chosen is None:
            log.error("Strategy did not find a suitable ticket")
            notify(
                "Ticketmaster Bot",
                "No matching tickets found",
                desktop=cfg.notifications.desktop,
                sound=cfg.notifications.sound,
            )
            return False

        # 6. Set quantity & add to cart
        await self._delay()
        await cart.set_quantity(page, cfg.tickets.quantity)
        await self._delay()

        added = await cart.add_to_cart(
            page,
            action_delay=(self.action_delay_min, self.action_delay_max),
            timeout_seconds=cfg.timing.page_timeout_seconds,
        )
        if not added:
            log.error("Failed to add tickets to cart")
            notify(
                "Ticketmaster Bot",
                "Failed to add tickets to cart",
                desktop=cfg.notifications.desktop,
                sound=cfg.notifications.sound,
            )
            return False

        notify(
            "Ticketmaster Bot",
            f"Tickets in cart! ({cfg.tickets.quantity}x section {chosen.section or '?'})",
            desktop=cfg.notifications.desktop,
            sound=cfg.notifications.sound,
        )

        # 7. Checkout
        success = await checkout.run_checkout(
            page,
            auto_purchase=cfg.checkout.auto_purchase,
            card_last_four=cfg.checkout.payment.card_last_four,
            action_delay=(self.action_delay_min, self.action_delay_max),
            preferred_delivery=cfg.checkout.delivery.preferred,
            allow_any_delivery=cfg.checkout.delivery.allow_any,
            expected_quantity=cfg.tickets.quantity,
            expected_candidate=chosen,
            price_tolerance=cfg.checkout.price_tolerance,
        )

        if cfg.checkout.auto_purchase and success:
            notify(
                "Ticketmaster Bot",
                "SUCCESS: Order placed!",
                desktop=cfg.notifications.desktop,
                sound=cfg.notifications.sound,
            )
        elif not cfg.checkout.auto_purchase:
            notify(
                "Ticketmaster Bot",
                "Cart ready - complete checkout manually!",
                desktop=cfg.notifications.desktop,
                sound=cfg.notifications.sound,
            )

        return success


async def _sleep_until_browser_closed(
    context,  # noqa: ANN001 - playwright type
    seconds: float,
) -> None:
    """Sleep up to ``seconds`` or until the user closes all pages."""
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            if not context.pages:
                return
        except Exception:  # noqa: BLE001
            return
        await asyncio.sleep(1.0)
