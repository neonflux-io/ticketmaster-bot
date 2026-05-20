"""Tests for the captcha ABC + dataclasses in ``src/captcha/base.py``.

These tests exercise real Python code paths only - no mocks, no fakes. A
``RecordingSolver`` real subclass exercises the ``CaptchaSolver`` ABC
contract; it is a real implementation that deterministically returns a
configured answer.
"""

from __future__ import annotations

import inspect
from dataclasses import fields, is_dataclass

import pytest

from src.captcha import (
    CaptchaChallenge,
    CaptchaSolution,
    CaptchaSolver,
)

# ---------------------------------------------------------------------------
# CaptchaChallenge / CaptchaSolution dataclass shape
# ---------------------------------------------------------------------------


def test_captcha_challenge_is_a_dataclass():
    assert is_dataclass(CaptchaChallenge)


def test_captcha_solution_is_a_dataclass():
    assert is_dataclass(CaptchaSolution)


def test_captcha_challenge_has_required_fields():
    names = {f.name for f in fields(CaptchaChallenge)}
    assert {
        "challenge_type",
        "screenshot_bytes",
        "refresh_callable",
        "input_locator",
        "page",
    } <= names


def test_captcha_solution_has_required_fields():
    names = {f.name for f in fields(CaptchaSolution)}
    assert {"text", "confidence", "provider", "latency_ms"} <= names


def test_captcha_challenge_can_be_constructed_with_minimum_args():
    challenge = CaptchaChallenge(
        challenge_type="yii_image",
        screenshot_bytes=b"\x89PNG\r\n",
        refresh_callable=None,
        input_locator=None,
        page=None,
    )
    assert challenge.challenge_type == "yii_image"
    assert challenge.screenshot_bytes == b"\x89PNG\r\n"
    assert challenge.refresh_callable is None
    assert challenge.input_locator is None
    assert challenge.page is None


def test_captcha_solution_constructed_with_optional_confidence():
    solution = CaptchaSolution(
        text="ABCDE",
        confidence=None,
        provider="openai_vlm",
        latency_ms=842,
    )
    assert solution.text == "ABCDE"
    assert solution.confidence is None
    assert solution.provider == "openai_vlm"
    assert solution.latency_ms == 842


def test_captcha_solution_accepts_confidence_float():
    solution = CaptchaSolution(text="ZZ", confidence=0.93, provider="x", latency_ms=10)
    assert solution.confidence == pytest.approx(0.93)


# ---------------------------------------------------------------------------
# CaptchaSolver ABC contract
# ---------------------------------------------------------------------------


def test_captcha_solver_solve_is_async():
    assert inspect.iscoroutinefunction(CaptchaSolver.solve)


def test_captcha_solver_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        CaptchaSolver()  # type: ignore[abstract]


def test_captcha_solver_supports_is_classmethod():
    # ``supports`` is declared as a classmethod that returns the set of
    # challenge_types this solver handles.
    assert "supports" in dir(CaptchaSolver)
    # It must be reachable as a classmethod (callable on the class).
    method = inspect.getattr_static(CaptchaSolver, "supports")
    assert isinstance(method, classmethod)


def test_captcha_solver_has_name_attribute():
    # Concrete solvers must expose a ``name`` attribute. The ABC declares it
    # so subclasses can override it as a class-level string.
    assert hasattr(CaptchaSolver, "name")


class _RecordingSolver(CaptchaSolver):
    """Real CaptchaSolver implementation that returns a configured answer.

    This is a real solver - it implements the ABC contract by deterministically
    returning a configured answer. Not a mock; it is a real subclass.
    """

    name = "recording"

    def __init__(self, answer: str, *, provider: str = "recording") -> None:
        self.answer = answer
        self.provider = provider
        self.calls: list[CaptchaChallenge] = []

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        self.calls.append(challenge)
        return CaptchaSolution(
            text=self.answer,
            confidence=1.0,
            provider=self.provider,
            latency_ms=1,
        )


async def test_concrete_subclass_can_solve_a_challenge():
    solver = _RecordingSolver(answer="HELLO")
    challenge = CaptchaChallenge(
        challenge_type="yii_image",
        screenshot_bytes=b"png",
        refresh_callable=None,
        input_locator=None,
        page=None,
    )
    solution = await solver.solve(challenge)
    assert solution is not None
    assert solution.text == "HELLO"
    assert solution.provider == "recording"
    assert solver.calls == [challenge]


def test_concrete_subclass_supports_returns_the_declared_types():
    assert _RecordingSolver.supports() == {"yii_image"}


def test_concrete_subclass_inherits_name_from_class():
    solver = _RecordingSolver(answer="X")
    assert solver.name == "recording"


def test_solver_subclass_missing_solve_cannot_instantiate():
    class _IncompleteSolver(CaptchaSolver):
        name = "incomplete"

        @classmethod
        def supports(cls) -> set[str]:
            return {"recaptcha"}

    with pytest.raises(TypeError):
        _IncompleteSolver()  # type: ignore[abstract]
