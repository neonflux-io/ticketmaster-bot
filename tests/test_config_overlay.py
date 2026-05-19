"""Tests for layered config resolution: profiles + events + --set + --explain.

Covers F1.4 / validation-contract assertions:
- refactor.config-explain
- refactor.config-profile
- refactor.config-set-cli
- refactor.config-env-expansion
- refactor.events-list
- refactor.events-legacy
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from src.cli import parse_set_overrides
from src.utils.config_loader import (
    _deep_merge,
    _expand_env_in_obj,
    config_to_yaml,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


# --- parse_set_overrides (pure helper) ------------------------------------


def test_parse_set_overrides_empty_list_returns_empty_dict():
    assert parse_set_overrides([]) == {}


def test_parse_set_overrides_simple_key_value():
    assert parse_set_overrides(["foo=bar"]) == {"foo": "bar"}


def test_parse_set_overrides_dotted_path_creates_nested_dict():
    result = parse_set_overrides(["tickets.quantity=4"])
    assert result == {"tickets": {"quantity": 4}}


def test_parse_set_overrides_multiple_paths_merge_into_single_tree():
    result = parse_set_overrides(
        ["tickets.quantity=4", "checkout.auto_purchase=false"]
    )
    assert result == {
        "tickets": {"quantity": 4},
        "checkout": {"auto_purchase": False},
    }


def test_parse_set_overrides_overlapping_paths_merge():
    result = parse_set_overrides(
        ["tickets.quantity=4", "tickets.strategy=cheapest"]
    )
    assert result == {"tickets": {"quantity": 4, "strategy": "cheapest"}}


def test_parse_set_overrides_type_inference_int():
    assert parse_set_overrides(["q=7"]) == {"q": 7}


def test_parse_set_overrides_type_inference_bool():
    assert parse_set_overrides(["x=true"]) == {"x": True}
    assert parse_set_overrides(["x=False"]) == {"x": False}


def test_parse_set_overrides_type_inference_string_passthrough():
    assert parse_set_overrides(["x=hello"]) == {"x": "hello"}


def test_parse_set_overrides_missing_equals_raises():
    with pytest.raises(ValueError, match="--set"):
        parse_set_overrides(["foo"])


def test_parse_set_overrides_empty_key_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_set_overrides(["=bar"])


def test_parse_set_overrides_url_with_equals_in_value():
    # The value side may contain '=' characters; only the first '=' splits.
    result = parse_set_overrides(["event.url=https://x.com/?a=b"])
    assert result == {"event": {"url": "https://x.com/?a=b"}}


# --- _deep_merge -----------------------------------------------------------


def test_deep_merge_replaces_scalars():
    assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_deep_merge_recurses_into_nested_dicts():
    base = {"a": {"b": 1, "c": 2}}
    overlay = {"a": {"b": 99}}
    assert _deep_merge(base, overlay) == {"a": {"b": 99, "c": 2}}


def test_deep_merge_lists_are_replaced_not_concatenated():
    base = {"x": [1, 2, 3]}
    overlay = {"x": [4]}
    assert _deep_merge(base, overlay) == {"x": [4]}


def test_deep_merge_adds_new_keys():
    assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    overlay = {"a": {"c": 2}}
    merged = _deep_merge(base, overlay)
    assert base == {"a": {"b": 1}}
    assert overlay == {"a": {"c": 2}}
    assert merged == {"a": {"b": 1, "c": 2}}


# --- _expand_env_in_obj (recursive) ----------------------------------------


def test_expand_env_in_obj_walks_dict(monkeypatch):
    monkeypatch.setenv("ACME_FOO", "bar")
    data = {"k": "${ACME_FOO}", "nested": {"inner": "${ACME_FOO}"}}
    assert _expand_env_in_obj(data) == {"k": "bar", "nested": {"inner": "bar"}}


def test_expand_env_in_obj_walks_list(monkeypatch):
    monkeypatch.setenv("ACME_FOO", "bar")
    data = {"events": [{"url": "${ACME_FOO}"}, {"url": "static"}]}
    out = _expand_env_in_obj(data)
    assert out["events"][0]["url"] == "bar"
    assert out["events"][1]["url"] == "static"


def test_expand_env_in_obj_passthrough_non_strings():
    data = {"n": 42, "b": True, "x": None}
    assert _expand_env_in_obj(data) == {"n": 42, "b": True, "x": None}


# --- load_config: profile overlay ------------------------------------------


def test_load_config_profile_fast_overrides_defaults(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        timing:
          humanize: true
          hold_open_seconds: 999
        """,
    )
    _write(
        tmp_path / "profiles" / "fast.yaml",
        """
        timing:
          humanize: false
          max_total_runtime_seconds: 600
          hold_open_seconds: 0
        """,
    )
    cfg = load_config(
        tmp_path / "config.yaml",
        tmp_path / "missing.yaml",
        profile="fast",
    )
    assert cfg.timing.humanize is False
    assert cfg.timing.max_total_runtime_seconds == 600
    assert cfg.timing.hold_open_seconds == 0


def test_load_config_profile_unknown_raises(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    with pytest.raises(FileNotFoundError, match="profile"):
        load_config(
            tmp_path / "config.yaml",
            tmp_path / "missing.yaml",
            profile="does_not_exist",
        )


def test_load_config_overrides_apply_after_profile(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    _write(
        tmp_path / "profiles" / "fast.yaml",
        """
        tickets:
          quantity: 1
        """,
    )
    cfg = load_config(
        tmp_path / "config.yaml",
        tmp_path / "missing.yaml",
        profile="fast",
        overrides={"tickets": {"quantity": 6}},
    )
    assert cfg.tickets.quantity == 6


def test_load_config_overrides_alone(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    cfg = load_config(
        tmp_path / "config.yaml",
        tmp_path / "missing.yaml",
        overrides={"checkout": {"auto_purchase": True}, "tickets": {"quantity": 3}},
    )
    assert cfg.checkout.auto_purchase is True
    assert cfg.tickets.quantity == 3


# --- load_config: events list normalization -------------------------------


def test_load_config_events_list_loads_multiple(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        events:
          - url: "https://www.ticketmaster.com/event/A"
          - url: "https://www.ticketmaster.com/event/B"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert len(cfg.events) == 2
    assert cfg.events[0].url.endswith("/event/A")
    assert cfg.events[1].url.endswith("/event/B")
    # Backwards-compat: cfg.event returns events[0]
    assert cfg.event is cfg.events[0]


def test_load_config_legacy_event_normalized_to_events_list(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/SOLO"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert len(cfg.events) == 1
    assert cfg.events[0].url.endswith("/event/SOLO")
    assert cfg.event is cfg.events[0]


def test_load_config_events_field_wins_when_both_present(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/LEGACY"
        events:
          - url: "https://www.ticketmaster.com/event/NEW"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert len(cfg.events) == 1
    assert cfg.events[0].url.endswith("/event/NEW")


def test_load_config_missing_event_and_events_raises(tmp_path):
    _write(tmp_path / "config.yaml", "tickets:\n  quantity: 2\n")
    with pytest.raises(ValueError, match=r"event"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


# --- env expansion through overlays ---------------------------------------


def test_load_config_env_expansion_in_event_url(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_EVENT_PATH", "https://www.ticketmaster.com/event/ENV")
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "${TM_EVENT_PATH}"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.events[0].url == "https://www.ticketmaster.com/event/ENV"


# --- config_to_yaml (used by --explain) -----------------------------------


def test_config_to_yaml_roundtrips(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        events:
          - url: "https://www.ticketmaster.com/event/A"
          - url: "https://www.ticketmaster.com/event/B"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    dumped = config_to_yaml(cfg)
    parsed = yaml.safe_load(dumped)
    assert isinstance(parsed, dict)
    assert "events" in parsed
    assert isinstance(parsed["events"], list)
    assert len(parsed["events"]) == 2
    assert parsed["events"][0]["url"].endswith("/event/A")
    # The legacy `event` key should NOT appear (normalized into events).
    assert "event" not in parsed


# --- ship the profile files we promised -----------------------------------


def test_shipped_profile_fast_exists_and_has_expected_values():
    fast = REPO_ROOT / "config" / "profiles" / "fast.yaml"
    assert fast.is_file()
    data = yaml.safe_load(fast.read_text())
    timing = data.get("timing") or {}
    assert timing.get("humanize") is False
    assert timing.get("max_total_runtime_seconds") == 600
    assert timing.get("hold_open_seconds") == 0


def test_shipped_profile_safe_exists_and_has_expected_values():
    safe = REPO_ROOT / "config" / "profiles" / "safe.yaml"
    assert safe.is_file()
    data = yaml.safe_load(safe.read_text())
    timing = data.get("timing") or {}
    checkout = data.get("checkout") or {}
    assert timing.get("humanize") is True
    assert timing.get("hold_open_seconds") == 1200
    assert checkout.get("auto_purchase") is False


# --- subprocess CLI tests (real run.py) -----------------------------------


def _run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "run.py", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_cli_explain_prints_valid_yaml_and_exits_zero():
    result = _run_cli("--explain")
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)
    assert "events" in parsed
    assert "tickets" in parsed
    assert "browser" in parsed


def test_cli_explain_with_profile_fast_reflects_profile_values():
    result = _run_cli("--profile", "fast", "--explain")
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert parsed["timing"]["humanize"] is False
    assert parsed["timing"]["max_total_runtime_seconds"] == 600
    assert parsed["timing"]["hold_open_seconds"] == 0


def test_cli_explain_with_set_overrides_applied():
    result = _run_cli(
        "--set", "tickets.quantity=4",
        "--set", "checkout.auto_purchase=false",
        "--explain",
    )
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert parsed["tickets"]["quantity"] == 4
    assert parsed["checkout"]["auto_purchase"] is False


def test_cli_explain_env_expansion_in_accounts(tmp_path, monkeypatch):
    # Write a temporary config + accounts pair so we don't touch the
    # committed defaults (or the user's real accounts.yaml).
    cfg_dir = tmp_path / "cfg"
    _write(
        cfg_dir / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    _write(
        cfg_dir / "accounts.yaml",
        """
        accounts:
          - name: "primary"
            email: "${TM_EMAIL}"
            password: "${TM_PASSWORD}"
        """,
    )
    result = _run_cli(
        "--config", str(cfg_dir / "config.yaml"),
        "--accounts", str(cfg_dir / "accounts.yaml"),
        "--explain",
        env_extra={"TM_EMAIL": "user@example.com", "TM_PASSWORD": "pw"},
    )
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(result.stdout)
    assert parsed["accounts"][0]["email"] == "user@example.com"
    # The literal placeholder should not appear anywhere.
    assert "${TM_EMAIL}" not in result.stdout


def test_cli_dry_run_with_multiple_events_logs_each(tmp_path):
    cfg_dir = tmp_path / "cfg"
    _write(
        cfg_dir / "config.yaml",
        """
        events:
          - url: "https://www.ticketmaster.com/event/A"
          - url: "https://www.ticketmaster.com/event/B"
        """,
    )
    result = _run_cli(
        "--config", str(cfg_dir / "config.yaml"),
        "--accounts", str(cfg_dir / "no-accounts.yaml"),
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    # Logger output lands on stderr; the URLs should be present once each.
    combined = result.stdout + result.stderr
    assert "/event/A" in combined
    assert "/event/B" in combined


def test_cli_dry_run_with_legacy_event_exits_zero(tmp_path):
    cfg_dir = tmp_path / "cfg"
    _write(
        cfg_dir / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/SOLO"
        """,
    )
    result = _run_cli(
        "--config", str(cfg_dir / "config.yaml"),
        "--accounts", str(cfg_dir / "no-accounts.yaml"),
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "/event/SOLO" in combined


def test_cli_set_invalid_format_exits_non_zero():
    result = _run_cli("--set", "noequals", "--explain")
    assert result.returncode != 0
    assert "--set" in (result.stderr + result.stdout)


def test_cli_missing_config_exits_two():
    result = _run_cli("--config", "/tmp/does_not_exist_xyz.yaml", "--dry-run")
    assert result.returncode == 2
