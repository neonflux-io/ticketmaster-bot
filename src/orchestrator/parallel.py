"""Parallel multi-account orchestration.

:class:`ParallelCoordinator` spawns one :class:`Runner` per account,
launches them concurrently (capped by ``max_parallel``) with a launch
``stagger_seconds`` between successive starts, and shares a single
``asyncio.Event`` -- :attr:`ParallelCoordinator.stop_event` -- across
every runner.

When any runner's ``run()`` returns ``True`` (the winner), the
coordinator:

1. Sets ``stop_event`` so any runner that consults it can short-circuit.
2. Cancels every sibling :class:`~asyncio.Task` via
   :meth:`asyncio.Task.cancel`.
3. Waits up to 10 s for the siblings to tear down their Playwright
   contexts cleanly (their flows catch :class:`~asyncio.CancelledError`
   inside the existing ``try`` / ``finally`` around
   ``context.close()``).
4. Returns ``True`` to the caller.

If every runner returns ``False`` (no winner), the coordinator returns
``False`` once they have all finished. The coordinator never starts
more runners than ``max_parallel`` and never starts a new runner after
``stop_event`` has been set.

The class is wired into :mod:`src.main` through the ``--parallel`` flag
so the CLI can run multiple accounts side-by-side. Tests inject a
custom ``runner_factory`` to swap the production
:class:`~src.vendors.ticketmaster.core.BotRunner` for a lightweight
fixture runner that exercises the same race semantics against a local
FastAPI server.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..utils.config_loader import AccountConfig, BotConfig

log = logging.getLogger("ticketmaster-bot")


class Runner(Protocol):
    """The minimal surface every parallel runner must expose.

    Implementations must:

    * Return ``True`` from ``run()`` on success (the coordinator treats
      this as the winning result and cancels every sibling runner).
    * Return ``False`` on a graceful no-op exit (sold out, no matching
      tickets, etc.).
    * Tear down any Playwright context they opened from inside a
      ``try`` / ``finally`` so a sibling-triggered cancellation
      (``asyncio.CancelledError`` raised mid-``await``) still closes
      Chromium cleanly.
    """

    async def run(self) -> bool: ...


# Factory signature kept open so tests can swap in their own runners.
# The default factory in :func:`default_runner_factory` ignores
# ``stop_event`` because the production BotRunner is driven entirely by
# task cancellation; subclasses that want voluntary early-out behaviour
# can poll the event themselves.
RunnerFactory = Callable[["AccountConfig", "BotConfig", asyncio.Event], Runner]


# Default cancellation grace window: how long we wait for sibling
# runners to tear down their Chromium contexts after the winner signals.
# The feature contract requires the loser to exit within 5 s of the
# event; doubling that gives Chromium-on-CI machines extra headroom.
_CANCEL_GRACE_SECONDS: float = 10.0


def default_runner_factory(
    account: AccountConfig,
    config: BotConfig,
    stop_event: asyncio.Event,  # noqa: ARG001 - kept for protocol parity
) -> Runner:
    """Build the production :class:`Runner` for ``account``.

    Resolves the ``ticketmaster`` :class:`~src.vendors.base.VendorAdapter`
    out of :mod:`src.registry.vendors` and asks it for a runner. The
    ``stop_event`` parameter is accepted for protocol parity with
    custom factories but unused: the production BotRunner relies on
    :meth:`asyncio.Task.cancel` for early termination.
    """
    # Import inside the function so the orchestrator module stays
    # cheap to import (the vendor adapter pulls Playwright transitively
    # via its core module).
    import src.vendors  # noqa: F401 -- registers TicketmasterAdapter

    from ..registry import vendors as vendor_registry

    adapter_cls = vendor_registry.get("ticketmaster")
    return adapter_cls().build_runner(config, account=account)


class ParallelCoordinator:
    """Race up to ``max_parallel`` :class:`Runner` instances.

    Parameters
    ----------
    accounts:
        The accounts to drive. ``len(accounts)`` runners are spawned;
        no more than ``max_parallel`` are live at any moment.
    config:
        Shared :class:`~src.utils.config_loader.BotConfig` passed to
        each runner through :data:`runner_factory`.
    max_parallel:
        Maximum concurrent runners. Excess accounts wait their turn
        behind an :class:`asyncio.Semaphore`. Must be ``>= 1``.
    stagger_seconds:
        Delay between successive runner launches. Applied *inside* the
        semaphore so we do not pile up sleeping tasks past the cap.
        Must be ``>= 0``. The first runner does not stagger.
    runner_factory:
        Optional override. Receives ``(account, config, stop_event)``
        and must return a :class:`Runner`. Defaults to
        :func:`default_runner_factory`.
    """

    def __init__(
        self,
        accounts: list[AccountConfig],
        config: BotConfig,
        *,
        max_parallel: int = 3,
        stagger_seconds: float = 5.0,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError(f"max_parallel must be >= 1, got {max_parallel}")
        if stagger_seconds < 0:
            raise ValueError(f"stagger_seconds must be >= 0, got {stagger_seconds}")
        if not accounts:
            raise ValueError("ParallelCoordinator requires at least one account")
        self._accounts: list[AccountConfig] = list(accounts)
        self._config = config
        self._max_parallel = int(max_parallel)
        self._stagger_seconds = float(stagger_seconds)
        self._runner_factory: RunnerFactory = (
            runner_factory if runner_factory is not None else default_runner_factory
        )
        # Public so callers can inject a watcher (tests, observability).
        self.stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public read-only views.
    # ------------------------------------------------------------------

    @property
    def accounts(self) -> list[AccountConfig]:
        return list(self._accounts)

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    @property
    def stagger_seconds(self) -> float:
        return self._stagger_seconds

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    async def _run_one(
        self,
        account: AccountConfig,
        index: int,
        sem: asyncio.Semaphore,
    ) -> bool:
        """Run one account, gated by ``sem`` to respect ``max_parallel``.

        Staggering is applied *after* acquiring the semaphore so a
        runner does not waste its concurrency slot sleeping; we sleep
        the equivalent budget once a slot is available instead. The
        first runner (``index == 0``) skips the stagger to avoid
        adding latency to the only-account-running case.

        Returns ``False`` if ``stop_event`` is already set when the
        runner reaches its launch point so a late-arriving slot does
        not start work after the race has been decided.
        """
        async with sem:
            if index > 0 and self._stagger_seconds > 0 and not self.stop_event.is_set():
                log.info(
                    "[%s] Staggering launch by %.1fs",
                    account.name,
                    self._stagger_seconds,
                )
                await asyncio.sleep(self._stagger_seconds)
            if self.stop_event.is_set():
                log.info(
                    "[%s] Skipping launch -- stop_event already set",
                    account.name,
                )
                return False
            runner = self._runner_factory(account, self._config, self.stop_event)
            log.info("[%s] Launching runner", account.name)
            try:
                result = await runner.run()
            except asyncio.CancelledError:
                log.info("[%s] Runner cancelled", account.name)
                raise
            except Exception:
                log.exception("[%s] Runner failed with exception", account.name)
                return False
            if result:
                log.info(
                    "[%s] Runner succeeded -- signalling stop_event",
                    account.name,
                )
                self.stop_event.set()
            else:
                log.info("[%s] Runner finished without success", account.name)
            return bool(result)

    async def _cancel_pending(self, pending: set[asyncio.Task[bool]]) -> None:
        """Cancel every task in ``pending`` and wait for it to exit.

        Gives the runners up to :data:`_CANCEL_GRACE_SECONDS` to
        unwind their ``try`` / ``finally`` blocks (Chromium context
        close, file flushes, etc.). If a runner ignores cancellation
        beyond that window we still return -- the coordinator keeps
        moving rather than wedging the parent task forever.
        """
        if not pending:
            return
        for task in pending:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=_CANCEL_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Some sibling runners did not finish teardown within %.1fs "
                "after the winner signalled stop",
                _CANCEL_GRACE_SECONDS,
            )

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    async def run(self) -> bool:
        """Race every account; return ``True`` once any runner succeeds.

        Cancellation flow:

        * Winner's task returns ``True`` -> ``stop_event`` is set inside
          :meth:`_run_one`.
        * The coordinator observes the winner via
          :func:`asyncio.wait` with ``FIRST_COMPLETED``.
        * Every still-pending task is cancelled and awaited (up to
          :data:`_CANCEL_GRACE_SECONDS`) so their Playwright contexts
          tear down before this method returns.

        If the *coordinator itself* is cancelled by its caller, every
        in-flight task is cancelled and awaited before the
        :class:`asyncio.CancelledError` is re-raised so no stray
        Chromium child outlives the parent task tree.
        """
        sem = asyncio.Semaphore(self._max_parallel)
        tasks: set[asyncio.Task[bool]] = {
            asyncio.create_task(
                self._run_one(account, i, sem),
                name=f"runner-{account.name}-{i}",
            )
            for i, account in enumerate(self._accounts)
        }

        pending: set[asyncio.Task[bool]] = set(tasks)
        winner_seen = False
        try:
            while pending and not winner_seen:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        log.warning(
                            "Runner %s raised %s",
                            task.get_name(),
                            exc.__class__.__name__,
                        )
                        continue
                    if task.result():
                        winner_seen = True
                        break

            if winner_seen:
                await self._cancel_pending(pending)
                return True
            # No winner; every task finished without raising. Nothing to
            # cancel because ``pending`` is empty.
            return False
        except asyncio.CancelledError:
            log.info(
                "Coordinator cancelled by caller -- tearing down %d in-flight runner task(s)",
                len(pending) + sum(1 for t in tasks if not t.done()),
            )
            still_running = {t for t in tasks if not t.done()}
            await self._cancel_pending(still_running)
            raise


__all__ = [
    "ParallelCoordinator",
    "Runner",
    "RunnerFactory",
    "default_runner_factory",
]
