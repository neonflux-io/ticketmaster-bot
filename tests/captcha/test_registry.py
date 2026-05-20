"""Tests for the captcha solver registry.

Mirrors the parametric/duplicate/missing pattern from
``tests/test_registries.py``. No mocks - tiny real ``CaptchaSolver``
subclasses are used as registered values.
"""

from __future__ import annotations

import pytest

from src.captcha import CaptchaChallenge, CaptchaSolution, CaptchaSolver
from src.captcha import registry as captcha_registry
from src.captcha.registry import CaptchaSolverRegistry
from src.registry.base import DuplicateRegistration, NotRegistered, Registry


class _StaticSolver(CaptchaSolver):
    """Real solver subclass - deterministically returns a configured answer."""

    name = "static"

    def __init__(self, answer: str = "OK") -> None:
        self.answer = answer

    @classmethod
    def supports(cls) -> set[str]:
        return {"yii_image"}

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaSolution | None:
        return CaptchaSolution(
            text=self.answer,
            confidence=1.0,
            provider=self.name,
            latency_ms=0,
        )


# ---------------------------------------------------------------------------
# Module-shape assertions (mirrors tests/test_registries.py)
# ---------------------------------------------------------------------------


def test_captcha_registry_module_exposes_register_get_all():
    assert hasattr(captcha_registry, "register")
    assert hasattr(captcha_registry, "get")
    assert hasattr(captcha_registry, "all")
    assert callable(captcha_registry.register)
    assert callable(captcha_registry.get)
    assert callable(captcha_registry.all)


def test_captcha_singleton_is_registry_instance():
    assert isinstance(captcha_registry.registry, Registry)


def test_captcha_registry_kind_is_captcha():
    assert captcha_registry.registry.kind == "captcha"


def test_captcha_registry_entry_point_group_is_namespaced():
    assert captcha_registry.registry.entry_point_group == "ticketmaster_bot.captcha"


def test_captcha_solver_registry_class_is_a_registry_subclass_or_alias():
    # CaptchaSolverRegistry is exported either as an alias for Registry[T]
    # or a thin subclass. Either way it must produce Registry-compatible
    # instances.
    reg = CaptchaSolverRegistry("captcha_test")
    assert isinstance(reg, Registry)
    assert reg.kind == "captcha_test"


# ---------------------------------------------------------------------------
# register / get / all
# ---------------------------------------------------------------------------


def test_register_then_get_returns_same_solver_class():
    reg = CaptchaSolverRegistry("captcha_isolated")
    reg.register("static", _StaticSolver)
    assert reg.get("static") is _StaticSolver


def test_all_returns_a_copy():
    reg = CaptchaSolverRegistry("captcha_isolated")
    reg.register("static", _StaticSolver)
    snapshot = reg.all()
    snapshot["other"] = object()
    assert "other" not in reg.all()


def test_all_includes_every_registered_solver():
    reg = CaptchaSolverRegistry("captcha_isolated")

    class _OtherSolver(_StaticSolver):
        name = "other"

    reg.register("static", _StaticSolver)
    reg.register("other", _OtherSolver)
    items = reg.all()
    assert items == {"static": _StaticSolver, "other": _OtherSolver}


# ---------------------------------------------------------------------------
# Duplicate / missing semantics (parametric over the singleton + a fresh reg)
# ---------------------------------------------------------------------------


def _fresh_registry() -> CaptchaSolverRegistry:
    return CaptchaSolverRegistry("captcha_isolated")


@pytest.mark.parametrize("registry_factory", [_fresh_registry])
def test_duplicate_register_raises_with_name_in_message(registry_factory):
    reg = registry_factory()
    reg.register("static", _StaticSolver)
    with pytest.raises(DuplicateRegistration) as exc_info:
        reg.register("static", _StaticSolver)
    msg = str(exc_info.value)
    assert "static" in msg
    assert exc_info.value.name == "static"
    assert exc_info.value.kind == reg.kind


@pytest.mark.parametrize("registry_factory", [_fresh_registry])
def test_missing_get_raises_with_requested_name_in_message(registry_factory):
    reg = registry_factory()
    with pytest.raises(NotRegistered) as exc_info:
        reg.get("does_not_exist")
    msg = str(exc_info.value)
    assert "does_not_exist" in msg
    assert exc_info.value.name == "does_not_exist"
    assert exc_info.value.kind == reg.kind


def test_singleton_duplicate_register_raises():
    # Use a unique key so the test is idempotent across reruns.
    key = "duplicate_probe__do_not_use"
    captcha_registry.register(key, _StaticSolver)
    try:
        with pytest.raises(DuplicateRegistration):
            captcha_registry.register(key, _StaticSolver)
    finally:
        captcha_registry.registry.unregister(key)


def test_singleton_missing_get_raises():
    with pytest.raises(NotRegistered):
        captcha_registry.get("nonexistent_captcha_solver_for_test_only")


def test_singleton_is_isolated_from_other_registries():
    from src.registry import notifiers, strategies

    key = "isolation_probe_captcha__do_not_use"
    captcha_registry.register(key, _StaticSolver)
    try:
        assert captcha_registry.get(key) is _StaticSolver
        with pytest.raises(NotRegistered):
            strategies.get(key)
        with pytest.raises(NotRegistered):
            notifiers.get(key)
    finally:
        captcha_registry.registry.unregister(key)
