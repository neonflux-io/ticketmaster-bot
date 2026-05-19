"""Core bot orchestrator - the state machine that drives everything."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import async_playwright

from ...hooks.base import HookRegistry
from ...humanize.profile import apply_profile
from ...orchestrator.lifecycle import LifecycleDispatcher
from ...proxy.manager import ProxyManager
from ...strategies.factory import build_strategy
from ...utils.notifier import notify
from ...utils.retry import maybe_human_delay
from ...utils.run_artifacts import RunArtifactDir, har_kwargs
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
        # Resolved at __init__ so a bad proxy URL fails early rather
        # than after we've spawned a Chromium child. ``ProxyManager``
        # construction validates URLs and the policy name.
        self.proxy_manager = ProxyManager(config.proxy)
        # Each runner owns its own hook registry + lifecycle dispatcher
        # so multi-account / parallel runs never share hook state.
        # Hooks are registered by the caller (CLI bootstrap, tests,
        # the multiplex bridge) before ``run()`` is awaited.
        self.hooks: HookRegistry = HookRegistry()
        self.lifecycle: LifecycleDispatcher = LifecycleDispatcher(self.hooks)
        # Populated lazily inside ``_run`` when HAR recording is on so
        # hooks / tests can resolve the per-run artefact directory.
        self.artifact_dir: RunArtifactDir | None = None

    async def _delay(self) -> None:
        await maybe_human_delay(self.humanize, self.action_delay_min, self.action_delay_max)

    async def run(self) -> bool:
        """Run the bot end-to-end. Returns True on success."""
        await self.lifecycle.fire(
            "on_run_start",
            self,
            account=self.account.name if self.account else None,
        )
        success = False
        try:
            success = await self._run()
            if not success:
                # Soft failure (graceful False return). Let observers
                # react before the run-end event fires.
                await self.lifecycle.fire(
                    "on_failure",
                    self,
                    error=None,
                    account=self.account.name if self.account else None,
                )
            return success
        except Exception as exc:
            await self.lifecycle.fire(
                "on_failure",
                self,
                error=exc,
                account=self.account.name if self.account else None,
            )
            raise
        finally:
            await self.lifecycle.fire(
                "on_run_end",
                self,
                success=success,
                account=self.account.name if self.account else None,
            )

    async def _run(self) -> bool:
        """Internal driver. The public :meth:`run` wraps this in lifecycle fires."""
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

        # Resolve the per-context fingerprint preset (desktop / mobile)
        # into the small set of overrides Playwright accepts. The
        # profile helper overwrites viewport + is_mobile + has_touch so
        # a mobile profile can't be diluted by the desktop-only values
        # computed above. A caller-set ``user_agent`` (from
        # stealth_cfg.user_agent) still wins because
        # ``apply_mobile_profile`` only sets the mobile UA when
        # ``user_agent`` is unset.
        profile_overrides: dict[str, object] = {"viewport": viewport}
        if user_agent is not None:
            profile_overrides["user_agent"] = user_agent
        apply_profile(cfg.browser.profile, profile_overrides)
        resolved_viewport: dict[str, int] = profile_overrides["viewport"]  # type: ignore[assignment]
        resolved_user_agent: str | None = profile_overrides.get("user_agent")  # type: ignore[assignment]
        is_mobile = bool(profile_overrides.get("is_mobile", False))
        has_touch = bool(profile_overrides.get("has_touch", False))

        # Resolve the proxy kwarg for this account before entering the
        # async context. The manager returns ``None`` when no proxy is
        # configured, in which case the kwarg is omitted entirely (rather
        # than passed as ``proxy=None``) because Playwright's typed
        # bindings reject ``None`` for that field on some versions.
        proxy_kwargs = self.proxy_manager.resolve(self.account.name if self.account else None)
        if proxy_kwargs is not None:
            log.info(
                "Using proxy %s for account %s",
                proxy_kwargs.get("server"),
                self.account.name if self.account else "<no-account>",
            )

        # When HAR recording is enabled, allocate the per-run artefact
        # directory up front so the path threaded into Playwright's
        # ``record_har_path`` kwarg lives under
        # ``logs/run-<UTC>-<account>/network.har``. The directory is
        # created lazily; passing the path to
        # ``launch_persistent_context`` is enough — Playwright creates
        # the file on first network event and finalises it on close.
        artifact_dir: RunArtifactDir | None = None
        har_launch_kwargs: dict[str, str] = {}
        if cfg.logging.artifacts.record_har:
            base_dir = Path(cfg.logging.file).parent if cfg.logging.file else Path("logs")
            artifact_dir = RunArtifactDir(
                base=base_dir,
                account_name=self.account.name if self.account else None,
            )
            artifact_dir.ensure()
            har_launch_kwargs = har_kwargs(artifact_dir.path("network.har"))
            log.info(
                "Recording HAR to %s",
                artifact_dir.path("network.har"),
            )
        # Expose the artefact dir so hooks/tests can resolve the run
        # path without re-computing it.
        self.artifact_dir = artifact_dir

        async with async_playwright() as p:
            log.info(
                "Launching browser (headless=%s, profile=%s)",
                cfg.browser.headless,
                cfg.browser.profile,
            )
            # --disable-blink-features=AutomationControlled removes one
            # automation tell. apply_stealth() handles the others.
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(user_data_dir),
                "headless": cfg.browser.headless,
                "slow_mo": cfg.browser.slow_mo_ms,
                "locale": cfg.browser.locale,
                "timezone_id": cfg.browser.timezone,
                "viewport": resolved_viewport,
                "user_agent": resolved_user_agent,
                "is_mobile": is_mobile,
                "has_touch": has_touch,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--disable-features=AutomationControlled",
                ],
            }
            if proxy_kwargs is not None:
                launch_kwargs["proxy"] = proxy_kwargs
            if har_launch_kwargs:
                launch_kwargs.update(har_launch_kwargs)
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
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
                await self.lifecycle.fire("before_login", self, account=self.account.name)
                await auth.login(
                    context,
                    self.account,
                    action_delay=(self.action_delay_min, self.action_delay_max),
                    typing_cfg=typing_cfg,
                )
                await self.lifecycle.fire("after_login", self, account=self.account.name)
            else:
                log.info("Existing session detected for %s", self.account.name)
        else:
            log.warning(
                "No account configured - assuming session persistence handles auth. "
                "Run with browser visible and log in manually if needed."
            )

        page = await context.new_page()

        # 2. Navigate to event page
        await self.lifecycle.fire("before_navigate", self, url=cfg.event.url)
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
            await self.lifecycle.fire("after_queue_release", self, state=state)
        else:
            # Fire ``after_queue_release`` even when no queue was present
            # so hooks that key off "the runner has reached the
            # tickets/sold_out/not_on_sale page" have a single
            # observation point.
            await self.lifecycle.fire("after_queue_release", self, state=state)

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
        await self.lifecycle.fire("before_select", self, strategy=cfg.tickets.strategy)
        chosen = await strategy.pick(page)
        await self.lifecycle.fire("after_select", self, candidate=chosen)
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

        await self.lifecycle.fire("before_cart", self, candidate=chosen)
        added = await cart.add_to_cart(
            page,
            action_delay=(self.action_delay_min, self.action_delay_max),
            timeout_seconds=cfg.timing.page_timeout_seconds,
        )
        await self.lifecycle.fire("after_cart", self, candidate=chosen, added=added)
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
        await self.lifecycle.fire("before_checkout", self, candidate=chosen)
        if cfg.checkout.auto_purchase:
            await self.lifecycle.fire("before_place_order", self, candidate=chosen)
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
