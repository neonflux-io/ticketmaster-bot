"""Captcha solver chain: try each provider in order, fall through on None.

A :class:`CaptchaSolverChain` is constructed from an ordered list of
``(provider_name, retries)`` tuples. ``solve_with_chain`` walks the list,
asks the registered solver up to its ``retries`` attempts, and returns the
first :class:`~src.captcha.base.CaptchaSolution` it gets. When every
configured attempt has been consumed without a successful solve, it
returns ``None`` and lets the caller fall back to a human-pause loop.

Every attempt is logged to the ``ticketmaster-bot`` logger with the
provider name, the per-call latency in milliseconds, and the outcome
(``solved`` / ``none`` / ``error``).
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from . import registry as captcha_registry
from .base import CaptchaChallenge, CaptchaSolution, CaptchaSolver

log = logging.getLogger("ticketmaster-bot")


@dataclass(frozen=True)
class _Step:
    """A single provider entry in the chain."""

    name: str
    retries: int


class CaptchaSolverChain:
    """Ordered chain of captcha solvers with per-provider retry budgets.

    Parameters
    ----------
    providers:
        Ordered sequence of ``(provider_name, retries)`` tuples. The chain
        looks each ``provider_name`` up in :data:`src.captcha.registry.registry`
        and asks the registered solver up to ``retries`` times. The first
        :class:`~src.captcha.base.CaptchaSolution` returned wins; ``None``
        and exceptions both count as a consumed attempt.
    """

    def __init__(self, providers: Sequence[tuple[str, int]]) -> None:
        self._steps: tuple[_Step, ...] = tuple(
            _Step(name=name, retries=max(0, int(retries))) for name, retries in providers
        )

    @property
    def steps(self) -> tuple[_Step, ...]:
        """Read-only view of the configured provider chain."""
        return self._steps

    async def solve_with_chain(
        self,
        challenge: CaptchaChallenge,
    ) -> CaptchaSolution | None:
        """Walk the provider chain and return the first successful solution.

        Returns ``None`` when the entire chain has been exhausted without a
        successful solve. The caller is responsible for the fallback (e.g.
        prompting a human to solve the captcha manually).

        Raises
        ------
        NotRegistered
            If any provider name in the chain is not registered in
            :mod:`src.captcha.registry`. The chain surfaces the configuration
            error immediately rather than silently skipping unknown
            providers.
        """
        for step in self._steps:
            solver = captcha_registry.get(step.name)
            for attempt in range(1, step.retries + 1):
                solution = await self._attempt(solver, step, attempt, challenge)
                if solution is not None:
                    return solution
        return None

    async def _attempt(
        self,
        solver: CaptchaSolver,
        step: _Step,
        attempt: int,
        challenge: CaptchaChallenge,
    ) -> CaptchaSolution | None:
        """Run a single provider attempt and log the outcome.

        Returns the produced :class:`CaptchaSolution`, or ``None`` if the
        solver could not solve this challenge (either by returning ``None``
        or by raising). Exceptions from the solver are caught and logged as
        a failed attempt - the chain treats them the same as a ``None``
        return so the caller can still fall through to the next provider.
        """
        solve = getattr(solver, "solve", None)
        if not callable(solve):
            log.warning(
                "captcha provider %r is not callable (attempt %d/%d); skipping",
                step.name,
                attempt,
                step.retries,
            )
            return None

        start = time.perf_counter()
        try:
            result = solve(challenge)
            if inspect.isawaitable(result):
                solution = await result
            else:
                solution = result
        except Exception as exc:  # noqa: BLE001 - we want to log + continue
            latency_ms = int((time.perf_counter() - start) * 1000)
            log.warning(
                "captcha provider=%s attempt=%d/%d latency=%dms outcome=error error=%s",
                step.name,
                attempt,
                step.retries,
                latency_ms,
                exc,
            )
            return None

        latency_ms = int((time.perf_counter() - start) * 1000)
        if solution is None:
            log.info(
                "captcha provider=%s attempt=%d/%d latency=%dms outcome=none",
                step.name,
                attempt,
                step.retries,
                latency_ms,
            )
            return None

        log.info(
            "captcha provider=%s attempt=%d/%d latency=%dms outcome=success",
            step.name,
            attempt,
            step.retries,
            latency_ms,
        )
        return solution


__all__ = ["CaptchaSolverChain"]
