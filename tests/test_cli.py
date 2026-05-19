"""Tests for the unified CLI in :mod:`src.cli` (F1.5).

These tests cover every flag combination the CLI accepts and verify the
contract assertions:

- ``refactor.cli-exit-codes``  --dry-run + bad --config + --no-headless + --no-auto-purchase
- ``refactor.cli-vendor``     --vendor must validate against the VendorRegistry

All CLI tests spawn a real ``python run.py`` subprocess so the namespace
+ asyncio bootstrap + load_config + final exit code are exercised end to
end. Pure-Python helpers (``build_parser``, ``parse_set_overrides``) have
unit tests in addition.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from src.cli import build_parser, parse_set_overrides

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def _run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
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


# ---------------------------------------------------------------------------
# Pure-Python: build_parser + namespace defaults.
# ---------------------------------------------------------------------------


def test_build_parser_returns_argument_parser():
    import argparse

    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_build_parser_defaults_cover_every_documented_flag():
    parser = build_parser()
    ns = parser.parse_args([])
    # Existing flags preserved.
    assert ns.config == "config/config.yaml"
    assert ns.accounts == "config/accounts.yaml"
    assert ns.account_name is None
    assert ns.dry_run is False
    assert ns.explain is False
    assert ns.headless is None  # mutually exclusive group default
    assert ns.auto_purchase is None
    # New F1.5 flags.
    assert ns.profile is None
    assert ns.set_overrides == []
    assert ns.events == []
    assert ns.vendor == "ticketmaster"
    assert ns.parallel is False
    assert ns.max_parallel is None
    assert ns.stagger_seconds is None


def test_build_parser_headless_pair_is_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--headless", "--no-headless"])


def test_build_parser_auto_purchase_pair_is_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--auto-purchase", "--no-auto-purchase"])


def test_build_parser_no_headless_sets_false():
    parser = build_parser()
    ns = parser.parse_args(["--no-headless"])
    assert ns.headless is False


def test_build_parser_headless_sets_true():
    parser = build_parser()
    ns = parser.parse_args(["--headless"])
    assert ns.headless is True


def test_build_parser_no_auto_purchase_sets_false():
    parser = build_parser()
    ns = parser.parse_args(["--no-auto-purchase"])
    assert ns.auto_purchase is False


def test_build_parser_max_parallel_must_be_positive_int():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--max-parallel", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--max-parallel", "-2"])
    ns = parser.parse_args(["--max-parallel", "4"])
    assert ns.max_parallel == 4


def test_build_parser_stagger_must_be_non_negative_float():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--stagger", "-1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--stagger", "abc"])
    ns = parser.parse_args(["--stagger", "1.5"])
    assert ns.stagger_seconds == 1.5
    ns = parser.parse_args(["--stagger", "0"])
    assert ns.stagger_seconds == 0.0


def test_build_parser_events_appends():
    parser = build_parser()
    ns = parser.parse_args(["--events", "https://a", "--events", "https://b,https://c"])
    assert ns.events == ["https://a", "https://b,https://c"]


def test_build_parser_set_appends():
    parser = build_parser()
    ns = parser.parse_args(["--set", "a=1", "--set", "b=2"])
    assert ns.set_overrides == ["a=1", "b=2"]


def test_parse_set_overrides_still_reexported():
    # Backwards compat for any caller still importing from src.cli.
    assert parse_set_overrides(["tickets.quantity=3"]) == {"tickets": {"quantity": 3}}


# ---------------------------------------------------------------------------
# Subprocess CLI tests: --vendor.
# ---------------------------------------------------------------------------


def test_cli_vendor_ticketmaster_dry_run_exits_zero():
    result = _run_cli("--vendor", "ticketmaster", "--dry-run")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_cli_vendor_unknown_exits_nonzero_with_name_in_stderr():
    result = _run_cli("--vendor", "nope", "--dry-run")
    assert result.returncode != 0
    # The vendor name must appear in stderr so the user can see what was rejected.
    assert "nope" in result.stderr


def test_cli_vendor_unknown_exits_nonzero_even_with_explain():
    # --explain still runs through vendor validation; an invalid vendor short-circuits.
    result = _run_cli("--vendor", "axs", "--explain")
    assert result.returncode != 0
    assert "axs" in result.stderr


def test_cli_vendor_default_is_ticketmaster():
    # Without --vendor, the default must be ticketmaster (the only shipped adapter).
    result = _run_cli("--dry-run")
    assert result.returncode == 0, (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# Subprocess CLI tests: invalid config path & invalid profile name.
# ---------------------------------------------------------------------------


def test_cli_missing_config_exits_two():
    result = _run_cli("--config", "/tmp/__nope_does_not_exist__.yaml", "--dry-run")
    assert result.returncode == 2


def test_cli_unknown_profile_exits_nonzero(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    result = _run_cli(
        "--config", str(tmp_path / "config.yaml"),
        "--accounts", str(tmp_path / "no-accounts.yaml"),
        "--profile", "does_not_exist",
        "--dry-run",
    )
    assert result.returncode != 0
    # Profile resolution lives in load_config; its error message names the profile.
    combined = result.stderr + result.stdout
    assert "does_not_exist" in combined


# ---------------------------------------------------------------------------
# Subprocess CLI tests: --headless / --no-headless / --auto-purchase / --no-auto-purchase.
# ---------------------------------------------------------------------------


def test_cli_no_headless_no_auto_purchase_explain_reflects_both():
    result = _run_cli("--no-headless", "--no-auto-purchase", "--explain")
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    assert parsed["browser"]["headless"] is False
    assert parsed["checkout"]["auto_purchase"] is False


def test_cli_headless_auto_purchase_explain_reflects_both():
    result = _run_cli("--headless", "--auto-purchase", "--explain")
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    assert parsed["browser"]["headless"] is True
    assert parsed["checkout"]["auto_purchase"] is True


def test_cli_headless_and_no_headless_together_argparse_rejects():
    # Mutually exclusive: argparse exits with code 2 on its own.
    result = _run_cli("--headless", "--no-headless", "--dry-run")
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Subprocess CLI tests: --events overrides.
# ---------------------------------------------------------------------------


def test_cli_events_single_url_overrides_config():
    result = _run_cli(
        "--events", "https://www.ticketmaster.com/event/FROM-CLI",
        "--explain",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    urls = [e["url"] for e in parsed["events"]]
    assert urls == ["https://www.ticketmaster.com/event/FROM-CLI"]


def test_cli_events_repeated_flag_yields_multiple_events():
    result = _run_cli(
        "--events", "https://www.ticketmaster.com/event/A",
        "--events", "https://www.ticketmaster.com/event/B",
        "--explain",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    urls = [e["url"] for e in parsed["events"]]
    assert urls == [
        "https://www.ticketmaster.com/event/A",
        "https://www.ticketmaster.com/event/B",
    ]


def test_cli_events_comma_separated_value():
    result = _run_cli(
        "--events", "https://www.ticketmaster.com/event/A,https://www.ticketmaster.com/event/B",
        "--explain",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    urls = [e["url"] for e in parsed["events"]]
    assert urls == [
        "https://www.ticketmaster.com/event/A",
        "https://www.ticketmaster.com/event/B",
    ]


# ---------------------------------------------------------------------------
# Subprocess CLI tests: --parallel / --max-parallel / --stagger.
# ---------------------------------------------------------------------------


def test_cli_parallel_flag_with_dry_run_exits_zero():
    result = _run_cli(
        "--parallel",
        "--max-parallel", "3",
        "--stagger", "0.5",
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    # The runtime logs the parallel parameters so the user can see what was applied.
    assert "Parallel" in combined or "parallel" in combined


def test_cli_max_parallel_zero_rejected():
    result = _run_cli("--max-parallel", "0", "--dry-run")
    assert result.returncode == 2  # argparse type validation


def test_cli_stagger_negative_rejected():
    result = _run_cli("--stagger", "-1", "--dry-run")
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Subprocess CLI tests: --account-name resolution.
# ---------------------------------------------------------------------------


def test_cli_account_name_unknown_exits_two(tmp_path):
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
            email: "u@example.com"
            password: "p"
        """,
    )
    result = _run_cli(
        "--config", str(tmp_path / "config.yaml"),
        "--accounts", str(tmp_path / "accounts.yaml"),
        "--account-name", "no_such_account",
        "--dry-run",
    )
    assert result.returncode == 2
    combined = result.stderr + result.stdout
    assert "no_such_account" in combined


def test_cli_account_name_known_dry_run_exits_zero(tmp_path):
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
            email: "u@example.com"
            password: "p"
        """,
    )
    result = _run_cli(
        "--config", str(tmp_path / "config.yaml"),
        "--accounts", str(tmp_path / "accounts.yaml"),
        "--account-name", "primary",
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


# ---------------------------------------------------------------------------
# Subprocess CLI tests: combined flag matrix.
# ---------------------------------------------------------------------------


def test_cli_full_flag_combination_dry_run(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/BASE"
        """,
    )
    result = _run_cli(
        "--config", str(tmp_path / "config.yaml"),
        "--accounts", str(tmp_path / "no-accounts.yaml"),
        "--vendor", "ticketmaster",
        "--profile", "fast",
        "--set", "tickets.quantity=4",
        "--events", "https://www.ticketmaster.com/event/FROM-CLI",
        "--no-headless",
        "--no-auto-purchase",
        "--parallel",
        "--max-parallel", "2",
        "--stagger", "0.25",
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_cli_full_flag_combination_explain(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/BASE"
        """,
    )
    result = _run_cli(
        "--config", str(tmp_path / "config.yaml"),
        "--accounts", str(tmp_path / "no-accounts.yaml"),
        "--vendor", "ticketmaster",
        "--profile", "fast",
        "--set", "tickets.quantity=4",
        "--set", "checkout.auto_purchase=false",
        "--events", "https://www.ticketmaster.com/event/FROM-CLI",
        "--explain",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    assert parsed["tickets"]["quantity"] == 4
    assert parsed["checkout"]["auto_purchase"] is False
    urls = [e["url"] for e in parsed["events"]]
    assert urls == ["https://www.ticketmaster.com/event/FROM-CLI"]
    # Profile 'fast' brings humanize=false.
    assert parsed["timing"]["humanize"]["enabled"] is False


# ---------------------------------------------------------------------------
# Subprocess CLI tests: --set parsing surfaces argparse-style error.
# ---------------------------------------------------------------------------


def test_cli_set_invalid_format_exits_nonzero():
    result = _run_cli("--set", "noequals", "--explain")
    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "--set" in combined


def test_cli_help_exits_zero():
    result = _run_cli("--help")
    assert result.returncode == 0
    # Confirm every documented flag appears in the help text.
    for flag in (
        "--config",
        "--accounts",
        "--account-name",
        "--profile",
        "--set",
        "--events",
        "--vendor",
        "--parallel",
        "--max-parallel",
        "--stagger",
        "--headless",
        "--no-headless",
        "--auto-purchase",
        "--no-auto-purchase",
        "--dry-run",
        "--explain",
    ):
        assert flag in result.stdout, f"missing {flag} in --help output"
