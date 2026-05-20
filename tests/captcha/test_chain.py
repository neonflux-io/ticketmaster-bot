"""Tests for ``CaptchaSolverChain`` in ``src/captcha/chain.py``.

No mocks - real ``CaptchaSolver`` subclasses with deterministic behaviour
exercise the chain's iteration, retry, and fall-through logic.
"""

from __future__ import annotations

import logging

import pytest

from src.captcha import CaptchaChallenge, CaptchaSolution, CaptchaSolver
from src.captcha import registry as captcha_registry
from src.captcha.chain import CaptchaSolverChain


def _make_challenge() -> CaptchaChallenge:
    return CaptchaChallenge(
        challenge_type="yii_image",
        screenshot_bytes=b"\x89PNG\r\n\x1a\n",
        refresh_callable=None,
        input_locator=None,
        page=None,
    )


class _SuccessSolver(CaptchaSolver):
    """Real solver that always returns a configured answer."""

    name = "success"

    def __init__(self, answer: str = "OK") -> None:
        self.answer = answer
        self.calls = 0

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image", "recaptcha", "akamai", "unknown"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        self.calls += 1
        return CaptchaSolution(
            text=self.answer,
            confidence=1.0,
            provider=self.name,
            latency_ms=1,
        )


class _AlwaysNoneSolver(CaptchaSolver):
    """Real solver that always returns None (failed to solve)."""

    name = "always_none"

    def __init__(self) -> None:
        self.calls = 0

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image", "recaptcha", "akamai", "unknown"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        self.calls += 1
        return None


class _SucceedAfterNSolver(CaptchaSolver):
    """Real solver that returns None for the first N calls then succeeds."""

    name = "succeed_after_n"

    def __init__(self, fail_count: int, answer: str = "DELAYED") -> None:
        self.fail_count = fail_count
        self.answer = answer
        self.calls = 0

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        self.calls += 1
        if self.calls <= self.fail_count:
            return None
        return CaptchaSolution(
            text=self.answer,
            confidence=0.9,
            provider=self.name,
            latency_ms=5,
        )


@pytest.fixture
def fresh_registry(monkeypatch):
    """Yield an isolated CaptchaSolverRegistry the chain reads from."""
    from src.captcha.registry import CaptchaSolverRegistry

    isolated = CaptchaSolverRegistry("captcha_chain_test")
    # Swap the module-level singleton for the duration of the test so
    # the chain looks providers up against our isolated registry.
    monkeypatch.setattr(captcha_registry, "registry", isolated)
    monkeypatch.setattr(captcha_registry, "register", isolated.register)
    monkeypatch.setattr(captcha_registry, "get", isolated.get)
    monkeypatch.setattr(captcha_registry, "all", isolated.all)
    return isolated


# ---------------------------------------------------------------------------
# First-success short-circuiting
# ---------------------------------------------------------------------------


async def test_chain_returns_first_successful_solution(fresh_registry):
    first = _SuccessSolver(answer="ONE")
    second = _SuccessSolver(answer="TWO")
    fresh_registry.register("first", first)
    fresh_registry.register("second", second)

    chain = CaptchaSolverChain(providers=[("first", 1), ("second", 1)])
    solution = await chain.solve_with_chain(_make_challenge())

    assert solution is not None
    assert solution.text == "ONE"
    assert first.calls == 1
    assert second.calls == 0  # second never tried after first succeeded


async def test_chain_falls_through_to_second_provider_when_first_returns_none(fresh_registry):
    failing = _AlwaysNoneSolver()
    success = _SuccessSolver(answer="WIN")
    fresh_registry.register("failing", failing)
    fresh_registry.register("success", success)

    chain = CaptchaSolverChain(providers=[("failing", 2), ("success", 1)])
    solution = await chain.solve_with_chain(_make_challenge())

    assert solution is not None
    assert solution.text == "WIN"
    # The failing solver was tried up to its retry budget.
    assert failing.calls == 2
    assert success.calls == 1


async def test_chain_respects_provider_retry_count(fresh_registry):
    eventually = _SucceedAfterNSolver(fail_count=2, answer="THIRD-TRY")
    fresh_registry.register("eventually", eventually)

    # Provider gets exactly 3 attempts; succeeds on the 3rd.
    chain = CaptchaSolverChain(providers=[("eventually", 3)])
    solution = await chain.solve_with_chain(_make_challenge())

    assert solution is not None
    assert solution.text == "THIRD-TRY"
    assert eventually.calls == 3


async def test_chain_exhausts_returns_none_when_no_provider_succeeds(fresh_registry):
    a = _AlwaysNoneSolver()
    b = _AlwaysNoneSolver()
    fresh_registry.register("a", a)
    fresh_registry.register("b", b)

    chain = CaptchaSolverChain(providers=[("a", 2), ("b", 3)])
    solution = await chain.solve_with_chain(_make_challenge())

    assert solution is None
    assert a.calls == 2
    assert b.calls == 3


async def test_chain_does_not_exceed_configured_retries(fresh_registry):
    """Provider whose retry budget is N and that never succeeds is called at most N times."""
    failing = _AlwaysNoneSolver()
    fresh_registry.register("failing", failing)

    chain = CaptchaSolverChain(providers=[("failing", 5)])
    solution = await chain.solve_with_chain(_make_challenge())

    assert solution is None
    assert failing.calls == 5


async def test_chain_iterates_providers_in_declared_order(fresh_registry):
    """First provider in the list is attempted before the second."""
    call_order: list[str] = []

    class _OrderedSolver(CaptchaSolver):
        name = "ordered"

        def __init__(self, tag: str) -> None:
            self.tag = tag

        @classmethod
        def supports(cls) -> set[str]:
            return {"yii_image"}

        async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
            call_order.append(self.tag)
            return None

    fresh_registry.register("alpha", _OrderedSolver(tag="alpha"))
    fresh_registry.register("beta", _OrderedSolver(tag="beta"))
    fresh_registry.register("gamma", _OrderedSolver(tag="gamma"))

    chain = CaptchaSolverChain(
        providers=[("gamma", 1), ("alpha", 1), ("beta", 1)],
    )
    await chain.solve_with_chain(_make_challenge())

    assert call_order == ["gamma", "alpha", "beta"]


async def test_chain_with_empty_provider_list_returns_none(fresh_registry):
    chain = CaptchaSolverChain(providers=[])
    solution = await chain.solve_with_chain(_make_challenge())
    assert solution is None


async def test_chain_logs_each_attempt_with_provider_and_outcome(fresh_registry, caplog):
    failing = _AlwaysNoneSolver()
    success = _SuccessSolver(answer="LOGGED")
    fresh_registry.register("failing", failing)
    fresh_registry.register("success", success)

    chain = CaptchaSolverChain(providers=[("failing", 2), ("success", 1)])

    with caplog.at_level(logging.DEBUG, logger="ticketmaster-bot"):
        await chain.solve_with_chain(_make_challenge())

    # Expect a record per attempt mentioning the provider name + outcome.
    records = [r for r in caplog.records if r.name == "ticketmaster-bot"]
    messages = [r.getMessage() for r in records]
    text = "\n".join(messages)
    assert "failing" in text
    assert "success" in text
    # Both failure and success outcomes are visible.
    assert any("fail" in m.lower() or "none" in m.lower() for m in messages)
    assert any("success" in m.lower() or "solved" in m.lower() for m in messages)
    # Latency is mentioned.
    assert any("ms" in m.lower() or "latency" in m.lower() for m in messages)


async def test_chain_raises_when_provider_name_is_not_registered(fresh_registry):
    """A misconfigured provider name surfaces as NotRegistered."""
    from src.registry.base import NotRegistered

    chain = CaptchaSolverChain(providers=[("ghost", 1)])
    with pytest.raises(NotRegistered):
        await chain.solve_with_chain(_make_challenge())


async def test_chain_treats_solver_raising_as_failed_attempt(fresh_registry):
    """If a solver raises, the chain logs and treats that attempt as a failure."""

    class _RaisingSolver(CaptchaSolver):
        name = "raising"

        def __init__(self) -> None:
            self.calls = 0

        @classmethod
        def supports(cls) -> set[str]:
            return {"yii_image"}

        async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
            self.calls += 1
            raise RuntimeError("simulated upstream timeout")

    raiser = _RaisingSolver()
    backup = _SuccessSolver(answer="BACKUP")
    fresh_registry.register("raiser", raiser)
    fresh_registry.register("backup", backup)

    chain = CaptchaSolverChain(providers=[("raiser", 3), ("backup", 1)])

    with caplog_at_warning():
        solution = await chain.solve_with_chain(_make_challenge())

    assert solution is not None
    assert solution.text == "BACKUP"
    # All 3 raising attempts consumed; backup ran once.
    assert raiser.calls == 3
    assert backup.calls == 1


def caplog_at_warning():
    """Real context manager wrapping caplog setup at WARNING."""
    import contextlib
    import logging

    logger = logging.getLogger("ticketmaster-bot")
    original_level = logger.level

    @contextlib.contextmanager
    def _cm():
        logger.setLevel(logging.DEBUG)
        try:
            yield
        finally:
            logger.setLevel(original_level)

    return _cm()
