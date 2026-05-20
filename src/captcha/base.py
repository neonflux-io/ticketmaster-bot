"""Captcha solver ABC + shared dataclasses.

This module ships the core abstractions for the captcha subsystem:

* :class:`CaptchaChallenge` - dataclass passed *into* a solver, carrying the
  challenge type, the screenshot bytes, an optional ``refresh_callable`` the
  solver may invoke to fetch a new challenge image, and references to the
  live Playwright ``page`` and the input ``locator`` so a solver can read
  page state without going through global lookups.
* :class:`CaptchaSolution` - dataclass returned *from* a solver, carrying the
  recognised text, an optional confidence score, the provider name (used in
  logs / Prometheus labels), and the round-trip latency in milliseconds.
* :class:`CaptchaSolver` - the abstract base class every concrete solver
  subclasses (e.g. ``OpenAIVLMSolver``). The ``supports()`` classmethod
  declares which ``challenge_type`` values the solver can handle; the
  ``solve()`` coroutine is the single entry point.

The dataclasses use ``from __future__ import annotations`` so the ``Locator``
and ``Page`` types declared by ``playwright.async_api`` are not imported
eagerly at module load time - that keeps tests that exercise only the pure
Python contract free of any Playwright dependency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page


#: Set of known challenge type identifiers. ``"unknown"`` is the fallback
#: a vendor uses when it has detected a captcha but cannot classify it.
ChallengeType = Literal["yii_image", "recaptcha", "akamai", "unknown"]


@dataclass
class CaptchaChallenge:
    """A captcha challenge handed to a solver.

    Attributes
    ----------
    challenge_type:
        One of ``"yii_image"``, ``"recaptcha"``, ``"akamai"`` or
        ``"unknown"``. The chain uses this to skip solvers that have not
        advertised support for the type, and metrics labels carry it through.
    screenshot_bytes:
        PNG / JPEG bytes of the captcha image. ``b""`` is allowed when a
        solver needs only the DOM context (e.g. for ``recaptcha``), but the
        common path is to pass a real screenshot of the captcha element.
    refresh_callable:
        Optional coroutine that, when awaited, asks the page to refresh the
        captcha (typically by clicking the "regenerate" button). The chain
        invokes this between retries when ``CaptchaConfig.refresh_between_attempts``
        is on. Sync callables are also accepted; the chain awaits the result
        only when it is awaitable.
    input_locator:
        Optional Playwright ``Locator`` pointing at the text input where the
        recognised solution should be typed. Solvers that perform the type
        themselves (rare) use it; the default flow types via the vendor's
        captcha integration after the chain returns.
    page:
        Optional reference to the live Playwright ``Page``. Solvers should
        prefer reading state through this rather than capturing it from a
        closure.
    """

    challenge_type: ChallengeType | str
    screenshot_bytes: bytes
    refresh_callable: Callable[[], Awaitable[None] | None] | None
    input_locator: Locator | None
    page: Page | None


@dataclass
class CaptchaSolution:
    """A recognised captcha answer returned from a solver.

    Attributes
    ----------
    text:
        The recognised captcha text (e.g. ``"ABCDE"``). The caller types this
        into the captcha input.
    confidence:
        Optional confidence score in ``[0.0, 1.0]``. ``None`` is acceptable
        for providers that do not surface a confidence value.
    provider:
        The name of the solver that produced this solution (e.g.
        ``"openai_vlm"``). Used in logs and metric labels.
    latency_ms:
        Wall-clock latency of the solve call, in integer milliseconds.
        Recorded by the chain so the overall budget is observable.
    """

    text: str
    confidence: float | None
    provider: str
    latency_ms: int


class CaptchaSolver(ABC):
    """Abstract base class for captcha solvers.

    Concrete subclasses MUST set the ``name`` class attribute and implement
    :meth:`solve`. They SHOULD override :meth:`supports` to declare which
    challenge types they handle - the base implementation returns the empty
    set, which means "this solver advertises support for nothing".

    The ABC declares ``name`` so static analysers see it; concrete classes
    override it with their public identifier (``"openai_vlm"``,
    ``"two_captcha"``, ...).
    """

    #: Human-readable identifier used in logs, metric labels, and config keys.
    #: Concrete subclasses MUST override this with a stable string.
    name: str = ""

    @classmethod
    def supports(cls) -> set[str]:
        """Return the set of challenge_type values this solver handles.

        The default implementation returns the empty set so subclasses are
        forced to declare their support explicitly. Concrete solvers
        override this with e.g. ``return {"yii_image"}``.
        """
        return set()

    @abstractmethod
    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        """Attempt to solve ``challenge``.

        Returns a :class:`CaptchaSolution` on success, or ``None`` when the
        solver could not produce an answer. Implementations MAY raise on
        transport-level failures (e.g. HTTP error from the upstream
        provider); the :class:`~src.captcha.chain.CaptchaSolverChain` treats
        both ``None`` and an exception as a failed attempt and consumes the
        provider's retry budget accordingly.
        """


__all__ = [
    "CaptchaChallenge",
    "CaptchaSolution",
    "CaptchaSolver",
    "ChallengeType",
]
