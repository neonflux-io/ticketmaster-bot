"""Tests for the real-money purchase kill switch."""

from __future__ import annotations

import pytest

from src.utils import purchase_guard
from src.utils.purchase_guard import (
    PURCHASE_OVERRIDE_ENV,
    PURCHASE_OVERRIDE_VALUE,
    PurchaseBlocked,
    gate,
    purchase_allowed,
)


def test_purchase_allowed_false_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PURCHASE_OVERRIDE_ENV, raising=False)
    assert purchase_allowed() is False


def test_purchase_allowed_false_with_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PURCHASE_OVERRIDE_ENV, "yes")
    assert purchase_allowed() is False


def test_purchase_allowed_false_with_close_but_wrong_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case sensitivity / typos / partial matches do not unlock the guard."""
    monkeypatch.setenv(PURCHASE_OVERRIDE_ENV, PURCHASE_OVERRIDE_VALUE.lower())
    assert purchase_allowed() is False


def test_purchase_allowed_true_with_exact_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PURCHASE_OVERRIDE_ENV, PURCHASE_OVERRIDE_VALUE)
    assert purchase_allowed() is True


def test_gate_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PURCHASE_OVERRIDE_ENV, raising=False)
    with pytest.raises(PurchaseBlocked) as excinfo:
        gate()
    msg = str(excinfo.value)
    assert PURCHASE_OVERRIDE_ENV in msg
    assert PURCHASE_OVERRIDE_VALUE in msg
    assert "place_order" in msg


def test_gate_raises_with_wrong_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PURCHASE_OVERRIDE_ENV, "definitely-not-the-magic-value")
    with pytest.raises(PurchaseBlocked):
        gate("place_order")


def test_gate_does_not_raise_with_exact_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PURCHASE_OVERRIDE_ENV, PURCHASE_OVERRIDE_VALUE)
    # No exception expected.
    gate("place_order")


def test_gate_passes_through_action_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PurchaseBlocked exception message identifies the gated action."""
    monkeypatch.delenv(PURCHASE_OVERRIDE_ENV, raising=False)
    with pytest.raises(PurchaseBlocked) as excinfo:
        gate("submit_order")
    assert "submit_order" in str(excinfo.value)


def test_module_exports() -> None:
    expected = {
        "PURCHASE_OVERRIDE_ENV",
        "PURCHASE_OVERRIDE_VALUE",
        "PurchaseBlocked",
        "gate",
        "purchase_allowed",
    }
    assert expected.issubset(set(purchase_guard.__all__))
    assert PURCHASE_OVERRIDE_ENV == "TICKETMASTER_BOT_PURCHASE_ALLOWED"
    assert PURCHASE_OVERRIDE_VALUE == "I_UNDERSTAND_REAL_MONEY"
