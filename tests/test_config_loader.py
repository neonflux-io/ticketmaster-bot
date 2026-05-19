"""Tests for src/utils/config_loader.py."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.utils.config_loader import (
    _coerce_optional_float,
    _expand_env,
    _parse_datetime,
    _validate_event_url,
    load_config,
)

# --- _parse_datetime ------------------------------------------------------


def test_parse_datetime_aware():
    dt = _parse_datetime("2026-06-01T10:00:00-05:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_naive_with_fallback_tz():
    dt = _parse_datetime("2026-06-01T10:00:00", fallback_tz="America/New_York")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_naive_without_fallback_raises():
    with pytest.raises(ValueError, match="naive"):
        _parse_datetime("2026-06-01T10:00:00", fallback_tz=None)


def test_parse_datetime_invalid_string():
    with pytest.raises(ValueError):
        _parse_datetime("not-a-date")


def test_parse_datetime_none():
    assert _parse_datetime(None) is None


# --- _coerce_optional_float ----------------------------------------------


def test_coerce_optional_float_none():
    assert _coerce_optional_float(None, "x") is None


def test_coerce_optional_float_string_numeric():
    assert _coerce_optional_float("100", "x") == 100.0


def test_coerce_optional_float_invalid_string():
    with pytest.raises(ValueError, match="must be numeric"):
        _coerce_optional_float("nope", "x")


# --- _expand_env ----------------------------------------------------------


def test_expand_env_replaces(monkeypatch):
    monkeypatch.setenv("ACME_FOO", "bar")
    assert _expand_env("${ACME_FOO}") == "bar"


def test_expand_env_passthrough_non_match():
    assert _expand_env("plain") == "plain"


def test_expand_env_non_string():
    assert _expand_env(42) == 42


# --- _validate_event_url --------------------------------------------------


def test_validate_event_url_trusted():
    _validate_event_url("https://www.ticketmaster.com/event/X", strict=True)


def test_validate_event_url_subdomain_trusted():
    _validate_event_url("https://concerts.livenation.com/x", strict=True)


def test_validate_event_url_untrusted_strict_raises():
    with pytest.raises(ValueError, match="not a known"):
        _validate_event_url("https://example.com/x", strict=True)


def test_validate_event_url_untrusted_warn_only(caplog):
    caplog.set_level("WARNING")
    _validate_event_url("https://example.com/x", strict=False)
    assert "not a known" in caplog.text


def test_validate_event_url_bad_scheme():
    with pytest.raises(ValueError, match="http"):
        _validate_event_url("ftp://example.com", strict=False)


# --- load_config end-to-end ----------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def test_load_config_minimal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.event.url.endswith("/event/X")
    assert cfg.tickets.quantity == 2
    assert cfg.checkout.auto_purchase is False
    assert cfg.timing.humanize is False
    assert cfg.timing.hold_open_seconds == 600.0
    assert cfg.browser.stealth.enabled is True


def test_load_config_invalid_log_level(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        logging:
          level: "VERBOSE"
        """,
    )
    with pytest.raises(ValueError, match="logging.level"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_invalid_max_price(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          max_price: "nope"
        """,
    )
    with pytest.raises(ValueError, match="max_price"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_strict_host_rejects(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://example.com/event/X"
          strict_host: true
        """,
    )
    with pytest.raises(ValueError, match="not a known"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_invalid_row_range(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "section_target"
          section_target:
            row_range: ["A"]
        """,
    )
    with pytest.raises(ValueError, match="row_range"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_accounts_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_PW", "s3cret")
    monkeypatch.setenv("ACME_EMAIL", "user@example.com")
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    _write(
        tmp_path / "accounts.yaml",
        """
        accounts:
          - name: "primary"
            email: "${ACME_EMAIL}"
            password: "${ACME_PW}"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "accounts.yaml")
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].email == "user@example.com"
    assert cfg.accounts[0].password == "s3cret"


def test_load_config_naive_datetime_localized(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
          on_sale_time: "2026-06-01T10:00:00"
        browser:
          timezone: "America/New_York"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.event.on_sale_time is not None
    assert cfg.event.on_sale_time.tzinfo is not None


def test_load_config_env_only_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_EMAIL", "env@example.com")
    monkeypatch.setenv("TM_PASSWORD", "envpw")
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].email == "env@example.com"
