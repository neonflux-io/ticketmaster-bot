"""Real-Chromium tests for the SG captcha auto-solve wiring (F9.3).

Drives the synthetic ``check_captcha_autosolve.html`` fixture (a
stripped-down clone of the SG ``/ticket/check-captcha/`` page whose
inline JS navigates to a sentinel URL when the visible answer
``ABCDE`` is typed) and verifies that:

* When ``CaptchaConfig.auto_solve`` is true and a chain is supplied,
  :func:`src.vendors.ticketmaster_sg.auth.wait_for_human_if_captcha`
  invokes the chain, fills the answer, clicks the submit button, and
  the URL transitions (so the helper returns ``True``).
* When ``CaptchaConfig.auto_solve`` is false, the chain is not even
  consulted — the existing human-pause path runs unchanged.
* :class:`src.vendors.ticketmaster_sg.core.BotRunner` builds the chain
  exactly as specified by ``CaptchaConfig`` and surfaces it via the
  ``_captcha_kwargs()`` hook for the cart/checkout/login call sites.

NO mocks: the deterministic-fake solver below is a real
:class:`~src.captcha.CaptchaSolver` subclass.
"""

from __future__ import annotations

import pytest

from src.captcha import CaptchaChallenge, CaptchaSolution, CaptchaSolver
from src.captcha import registry as captcha_registry
from src.captcha.chain import CaptchaSolverChain
from src.utils.config_loader import (
    AccountConfig,
    BotConfig,
    BrowserConfig,
    CaptchaConfig,
    CheckoutConfig,
    DeliveryConfig,
    EventConfig,
    LoggingConfig,
    NotificationsConfig,
    PaymentConfig,
    ProxyConfig,
    StealthConfig,
    TicketsConfig,
    TimingConfig,
)
from src.vendors.ticketmaster_sg import auth as sg_auth
from src.vendors.ticketmaster_sg.core import BotRunner as SGBotRunner

# The captcha fixture answers "ABCDE" — this is the value the fake
# solver returns and the fixture's inline JS checks for.
EXPECTED_ANSWER = "ABCDE"

FIXTURE_RELPATH = "vendors/ticketmaster_sg/check_captcha_autosolve.html"
NO_CAPTCHA_FIXTURE_RELPATH = "vendors/ticketmaster_sg/event_detail.html"


# ---------------------------------------------------------------------------
# Deterministic-fake provider — a real CaptchaSolver subclass.
# ---------------------------------------------------------------------------


class DeterministicAnswerSolver(CaptchaSolver):
    """A real solver that returns a configured answer.

    Used by the auto-solve test so the chain produces the exact answer
    the fixture's inline JS treats as "correct". Records every call so
    the test can assert call counts.
    """

    name = "deterministic_fake"

    def __init__(self, answer: str = EXPECTED_ANSWER) -> None:
        self.answer = answer
        self.calls: list[CaptchaChallenge] = []

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        self.calls.append(challenge)
        return CaptchaSolution(
            text=self.answer,
            confidence=1.0,
            provider=self.name,
            latency_ms=1,
        )


class AlwaysNoneSolver(CaptchaSolver):
    """A real solver that never produces an answer."""

    name = "always_none"

    def __init__(self) -> None:
        self.calls = 0

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        self.calls += 1
        return None


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_captcha_registry(monkeypatch):
    """Yield an isolated captcha registry the chain (and BotRunner) read from.

    The SG BotRunner looks every provider up via
    :func:`src.captcha.registry.get`, so we swap the module-level
    helpers for the duration of the test. This mirrors the pattern used
    by ``tests/captcha/test_chain.py``.
    """
    from src.captcha.registry import CaptchaSolverRegistry

    isolated: CaptchaSolverRegistry = CaptchaSolverRegistry("captcha_autosolve_test")
    monkeypatch.setattr(captcha_registry, "registry", isolated)
    monkeypatch.setattr(captcha_registry, "register", isolated.register)
    monkeypatch.setattr(captcha_registry, "get", isolated.get)
    monkeypatch.setattr(captcha_registry, "all", isolated.all)
    return isolated


def _make_bot_config(*, auto_solve: bool, providers: list[str]) -> BotConfig:
    """Build a minimal :class:`BotConfig` carrying the requested captcha block."""
    return BotConfig(
        events=[
            EventConfig(url="https://ticketmaster.sg/activity/detail/26sg_pglcs2major"),
        ],
        tickets=TicketsConfig(),
        checkout=CheckoutConfig(
            auto_purchase=False,
            payment=PaymentConfig(),
            delivery=DeliveryConfig(),
        ),
        timing=TimingConfig(),
        logging=LoggingConfig(file=None),
        notifications=NotificationsConfig(),
        browser=BrowserConfig(
            headless=True,
            user_data_dir="sessions/sg-captcha-test",
            stealth=StealthConfig(enabled=False),
        ),
        accounts=[AccountConfig(email="x@example.com", password="x", name="test")],
        proxy=ProxyConfig(),
        captcha=CaptchaConfig(
            auto_solve=auto_solve,
            providers=providers,
            retries_per_provider=3,
            refresh_between_attempts=True,
            captcha_timeout_seconds=300.0,
        ),
    )


# ---------------------------------------------------------------------------
# wait_for_human_if_captcha auto-solve path
# ---------------------------------------------------------------------------


async def test_wait_for_captcha_auto_solves_via_chain_on_yii_fixture(
    chromium_context, fixture_url, fresh_captcha_registry
) -> None:
    """Chain returns the right answer → input filled, submit clicked, URL transitions."""
    solver = DeterministicAnswerSolver(answer=EXPECTED_ANSWER)
    fresh_captcha_registry.register("deterministic_fake", solver)
    chain = CaptchaSolverChain(providers=[("deterministic_fake", 1)])

    page = await chromium_context.new_page()
    await page.goto(fixture_url(FIXTURE_RELPATH))
    # Sanity: the fixture mounts the Yii captcha image + input.
    assert await sg_auth._yii_captcha_visible(page) is True
    initial_url = page.url

    result = await sg_auth.wait_for_human_if_captcha(
        page,
        timeout_seconds=2.0,
        captcha_solver_chain=chain,
        refresh_between_attempts=False,
    )

    assert result is True
    assert len(solver.calls) == 1
    # The chain's solution was typed into the captcha input *before*
    # the submit happened; after the fixture's JS removes the box we
    # only have the URL transition to inspect.
    assert page.url != initial_url, "Expected URL transition after auto-solve"
    assert page.url.endswith("#captcha-passed")
    # And the page no longer carries the Yii captcha mount.
    assert await sg_auth._yii_captcha_visible(page) is False


async def test_wait_for_captcha_skips_chain_when_no_captcha_mounted(
    chromium_context, fixture_url, fresh_captcha_registry
) -> None:
    """A page without a captcha returns True immediately; chain is not consulted."""
    solver = DeterministicAnswerSolver(answer=EXPECTED_ANSWER)
    fresh_captcha_registry.register("deterministic_fake", solver)
    chain = CaptchaSolverChain(providers=[("deterministic_fake", 1)])

    page = await chromium_context.new_page()
    await page.goto(fixture_url(NO_CAPTCHA_FIXTURE_RELPATH))

    result = await sg_auth.wait_for_human_if_captcha(
        page,
        timeout_seconds=0.5,
        captcha_solver_chain=chain,
        refresh_between_attempts=False,
    )

    assert result is True
    assert solver.calls == []  # chain was not consulted


async def test_wait_for_captcha_falls_through_to_human_pause_when_chain_exhausts(
    chromium_context, fixture_url, fresh_captcha_registry
) -> None:
    """Chain returns None → existing human-pause loop runs (and times out)."""
    failing = AlwaysNoneSolver()
    fresh_captcha_registry.register("always_none", failing)
    chain = CaptchaSolverChain(providers=[("always_none", 2)])

    page = await chromium_context.new_page()
    await page.goto(fixture_url(FIXTURE_RELPATH))
    assert await sg_auth._yii_captcha_visible(page) is True

    result = await sg_auth.wait_for_human_if_captcha(
        page,
        timeout_seconds=0.5,
        captcha_solver_chain=chain,
        refresh_between_attempts=False,
    )

    # Chain exhausts → human-pause loop runs → times out → returns False.
    assert result is False
    # The chain DID consume both attempts of the failing provider.
    assert failing.calls == 2


async def test_wait_for_captcha_without_chain_uses_human_pause_only(
    chromium_context, fixture_url
) -> None:
    """Original (chain-less) call path is unchanged — returns False on timeout."""
    page = await chromium_context.new_page()
    await page.goto(fixture_url(FIXTURE_RELPATH))

    result = await sg_auth.wait_for_human_if_captcha(page, timeout_seconds=0.5)

    assert result is False


# ---------------------------------------------------------------------------
# BotRunner.__init__ — chain is (or is not) built per CaptchaConfig.auto_solve
# ---------------------------------------------------------------------------


def test_bot_runner_builds_chain_when_auto_solve_true(fresh_captcha_registry) -> None:
    """When ``captcha.auto_solve`` is true the runner exposes a configured chain."""
    fresh_captcha_registry.register(
        "deterministic_fake", DeterministicAnswerSolver(answer=EXPECTED_ANSWER)
    )

    cfg = _make_bot_config(auto_solve=True, providers=["deterministic_fake"])
    runner = SGBotRunner(cfg)

    assert runner.captcha_solver_chain is not None
    assert isinstance(runner.captcha_solver_chain, CaptchaSolverChain)
    step_names = [step.name for step in runner.captcha_solver_chain.steps]
    assert step_names == ["deterministic_fake"]
    assert runner.captcha_solver_chain.steps[0].retries == 3

    # And the kwargs that step modules consume reflect the config.
    kwargs = runner._captcha_kwargs()
    assert kwargs["captcha_solver_chain"] is runner.captcha_solver_chain
    assert kwargs["captcha_refresh_between_attempts"] is True
    assert kwargs["captcha_timeout_seconds"] == 300.0


def test_bot_runner_does_not_build_chain_when_auto_solve_false(
    fresh_captcha_registry,
) -> None:
    """When ``captcha.auto_solve`` is false the runner returns no chain at all."""
    cfg = _make_bot_config(auto_solve=False, providers=["deterministic_fake"])
    runner = SGBotRunner(cfg)

    assert runner.captcha_solver_chain is None
    # And the kwargs hook returns an empty dict so the historic call
    # signatures of auth.login / cart.add_to_cart / checkout.run_checkout
    # remain unchanged.
    assert runner._captcha_kwargs() == {}


def test_bot_runner_passes_chain_through_kwargs_hook(fresh_captcha_registry) -> None:
    """The chain plumbed through ``_captcha_kwargs`` matches the constructed chain."""
    fresh_captcha_registry.register(
        "deterministic_fake", DeterministicAnswerSolver(answer=EXPECTED_ANSWER)
    )
    cfg = _make_bot_config(auto_solve=True, providers=["deterministic_fake"])
    runner = SGBotRunner(cfg)

    # Walking the runner's kwargs (used by cart/checkout/login call sites)
    # surfaces the same chain object the runner constructed.
    kwargs = runner._captcha_kwargs()
    assert kwargs["captcha_solver_chain"] is runner.captcha_solver_chain


# ---------------------------------------------------------------------------
# End-to-end: BotRunner's chain on the synthetic captcha fixture
# ---------------------------------------------------------------------------


async def test_bot_runner_chain_solves_captcha_on_synthetic_fixture(
    chromium_context, fixture_url, fresh_captcha_registry
) -> None:
    """The chain built by ``SGBotRunner.__init__`` actually solves the captcha."""
    solver = DeterministicAnswerSolver(answer=EXPECTED_ANSWER)
    fresh_captcha_registry.register("deterministic_fake", solver)

    cfg = _make_bot_config(auto_solve=True, providers=["deterministic_fake"])
    runner = SGBotRunner(cfg)
    assert runner.captcha_solver_chain is not None

    page = await chromium_context.new_page()
    await page.goto(fixture_url(FIXTURE_RELPATH))
    pre_url = page.url

    result = await sg_auth.wait_for_human_if_captcha(
        page,
        timeout_seconds=2.0,
        captcha_solver_chain=runner.captcha_solver_chain,
        refresh_between_attempts=runner.config.captcha.refresh_between_attempts,
    )

    assert result is True
    assert len(solver.calls) == 1
    assert page.url != pre_url


async def test_bot_runner_no_chain_preserves_human_pause_behavior(
    chromium_context, fixture_url, fresh_captcha_registry
) -> None:
    """With auto_solve=False, the chain is not consulted — the no-chain timeout path runs."""
    fresh_captcha_registry.register(
        "deterministic_fake", DeterministicAnswerSolver(answer=EXPECTED_ANSWER)
    )

    cfg = _make_bot_config(auto_solve=False, providers=["deterministic_fake"])
    runner = SGBotRunner(cfg)
    assert runner.captcha_solver_chain is None

    page = await chromium_context.new_page()
    await page.goto(fixture_url(FIXTURE_RELPATH))

    # We pass NO chain (mirroring the kwargs hook returning {}).
    result = await sg_auth.wait_for_human_if_captcha(
        page,
        timeout_seconds=0.5,
        **runner._captcha_kwargs(),
    )

    assert result is False
    # The page still carries the Yii captcha — no automated submission
    # was attempted because no chain was constructed.
    assert await sg_auth._yii_captcha_visible(page) is True


# ---------------------------------------------------------------------------
# Captcha challenge structure: refresh_callable + input_locator are present
# ---------------------------------------------------------------------------


async def test_chain_receives_challenge_with_refresh_and_input_locator(
    chromium_context, fixture_url, fresh_captcha_registry
) -> None:
    """The challenge handed to the solver carries a refresh_callable + input locator."""
    captured: list[CaptchaChallenge] = []

    class _CapturingSolver(CaptchaSolver):
        name = "capturing"

        @classmethod
        def supports(cls) -> set[str]:
            return {"yii_image"}

        async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
            captured.append(challenge)
            return CaptchaSolution(
                text=EXPECTED_ANSWER,
                confidence=1.0,
                provider=self.name,
                latency_ms=1,
            )

    fresh_captcha_registry.register("capturing", _CapturingSolver())
    chain = CaptchaSolverChain(providers=[("capturing", 1)])

    page = await chromium_context.new_page()
    await page.goto(fixture_url(FIXTURE_RELPATH))

    result = await sg_auth.wait_for_human_if_captcha(
        page,
        timeout_seconds=2.0,
        captcha_solver_chain=chain,
        refresh_between_attempts=True,
    )
    assert result is True
    assert len(captured) == 1

    challenge = captured[0]
    assert challenge.challenge_type == "yii_image"
    assert challenge.screenshot_bytes  # non-empty PNG bytes
    assert challenge.input_locator is not None
    assert challenge.page is page
    # The refresh_callable must be awaitable (the chain awaits it).
    assert callable(challenge.refresh_callable)
