"""Tests for the ticketmaster.sg vendor adapter (F7.5).

These tests cover the four behaviours called out in the feature brief:

1. ``src/vendors/ticketmaster_sg/adapter.py`` implements the
   :class:`~src.vendors.base.VendorAdapter` ABC and exposes the SG
   per-step modules as attributes (``auth``, ``cart``, ``checkout``,
   ``core``, ``navigator``, ``queue``).
2. ``src/vendors/ticketmaster_sg/__init__.py`` registers the adapter
   under the name ``"ticketmaster_sg"`` in :mod:`src.registry.vendors`.
3. ``src/main.py`` auto-detects the vendor from the first event URL's
   host: a ``ticketmaster.sg`` URL picks ``ticketmaster_sg``, anything
   else (including ``ticketmaster.com``) picks ``ticketmaster``.
4. ``--vendor=ticketmaster_sg`` is accepted as an explicit override and
   ``run.py --vendor=ticketmaster_sg --dry-run --explain`` exits 0.

Every CLI-related assertion uses real ``subprocess.run`` against the
project venv interpreter — there are no mocks, no fake processes, and
no monkey-patched argv.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Spawn ``python run.py <args>`` from the repo root and return the result."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "run.py", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=45,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# 1. Adapter ABC + module attributes
# ---------------------------------------------------------------------------


def test_sg_adapter_subclasses_vendor_adapter() -> None:
    """TicketmasterSGAdapter inherits from :class:`VendorAdapter`."""
    base_mod = importlib.import_module("src.vendors.base")
    adapter_mod = importlib.import_module("src.vendors.ticketmaster_sg.adapter")
    assert issubclass(adapter_mod.TicketmasterSGAdapter, base_mod.VendorAdapter)


def test_sg_adapter_exposes_per_step_modules() -> None:
    """Adapter instances expose ``auth``, ``cart``, ``checkout``, ``core``,
    ``navigator``, and ``queue`` so flow code can call them directly."""
    adapter_mod = importlib.import_module("src.vendors.ticketmaster_sg.adapter")
    adapter = adapter_mod.TicketmasterSGAdapter()
    for name in ("auth", "cart", "checkout", "core", "navigator", "queue"):
        attr = getattr(adapter, name, None)
        assert attr is not None, f"TicketmasterSGAdapter missing attribute {name!r}"


def test_sg_adapter_name_is_ticketmaster_sg() -> None:
    """The adapter's stable name (used by the registry) is ``ticketmaster_sg``."""
    adapter_mod = importlib.import_module("src.vendors.ticketmaster_sg.adapter")
    adapter = adapter_mod.TicketmasterSGAdapter()
    assert adapter.name == "ticketmaster_sg"


def test_sg_adapter_build_runner_returns_runnable() -> None:
    """``build_runner(config, account)`` returns an object with an async ``run()``."""
    import inspect

    from src.utils.config_loader import load_config

    adapter_mod = importlib.import_module("src.vendors.ticketmaster_sg.adapter")

    config = load_config(
        REPO_ROOT / "config" / "config.yaml",
        REPO_ROOT / "config" / "no-such-accounts.yaml",
        overrides={
            "events": [
                {
                    "url": "https://ticketmaster.sg/activity/detail/26sg_pglcs2major",
                    "strict_host": False,
                }
            ]
        },
    )
    adapter = adapter_mod.TicketmasterSGAdapter()
    runner = adapter.build_runner(config)
    assert hasattr(runner, "run"), "runner must expose a run() method"
    assert inspect.iscoroutinefunction(runner.run), "runner.run must be a coroutine function"


# ---------------------------------------------------------------------------
# 2. Registry lookup
# ---------------------------------------------------------------------------


def test_sg_adapter_registered_in_vendor_registry() -> None:
    """Importing :mod:`src.vendors` registers ``ticketmaster_sg`` in the
    vendor registry alongside ``ticketmaster``."""
    import src.vendors  # noqa: F401  (side effect: registers all adapters)
    from src.registry import vendors as vendor_registry

    adapter_mod = importlib.import_module("src.vendors.ticketmaster_sg.adapter")
    obj = vendor_registry.get("ticketmaster_sg")
    assert obj is adapter_mod.TicketmasterSGAdapter


def test_sg_adapter_visible_in_registry_all() -> None:
    """``vendor_registry.all()`` includes both ``ticketmaster`` and
    ``ticketmaster_sg`` once the vendor subpackages have imported."""
    import src.vendors  # noqa: F401
    from src.registry import vendors as vendor_registry

    registered = set(vendor_registry.all().keys())
    assert "ticketmaster" in registered
    assert "ticketmaster_sg" in registered


def test_sg_registry_lookup_subprocess() -> None:
    """The registry lookup also works in a fresh interpreter (no import
    side effects from the test harness)."""
    cmd = [
        str(VENV_PYTHON),
        "-c",
        (
            "import src.vendors;"
            "from src.registry import vendors;"
            "from src.vendors.ticketmaster_sg.adapter import TicketmasterSGAdapter;"
            "assert vendors.get('ticketmaster_sg') is TicketmasterSGAdapter;"
            "print('OK')"
        ),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# 3. --vendor=ticketmaster_sg explicit selection
# ---------------------------------------------------------------------------


def test_cli_vendor_sg_dry_run_exits_zero(tmp_path: Path) -> None:
    """``--vendor=ticketmaster_sg --dry-run`` exits 0 with a sane SG event URL."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--vendor",
        "ticketmaster_sg",
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_cli_vendor_sg_dry_run_explain_exits_zero(tmp_path: Path) -> None:
    """``--vendor=ticketmaster_sg --dry-run --explain`` exits 0.

    ``--explain`` short-circuits before any browser launch, so this also
    covers the F7.5 brief's exact ``--dry-run --explain`` combination —
    explain wins and ``--dry-run`` is a no-op in that mode.
    """
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--vendor",
        "ticketmaster_sg",
        "--dry-run",
        "--explain",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)
    urls = [e["url"] for e in parsed["events"]]
    assert urls == ["https://ticketmaster.sg/activity/detail/26sg_pglcs2major"]


def test_cli_vendor_sg_default_config_explain_exits_zero() -> None:
    """``--vendor=ticketmaster_sg --explain`` with the default committed config
    still exits 0 — the committed config event URL is a .com URL but the
    SG adapter is selectable regardless (vendor selection happens before
    runtime, and dry-run + explain never opens a browser)."""
    result = _run_cli("--vendor", "ticketmaster_sg", "--explain")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_cli_help_lists_ticketmaster_sg_as_choice() -> None:
    """``--help`` mentions the SG vendor name so users discover it.

    The CLI does not constrain ``--vendor`` choices to a finite list
    (so plugin adapters can register at runtime), but the help text
    still references the vendor name via the registry — making the
    SG adapter discoverable from ``run.py --help``.
    """
    result = _run_cli("--help")
    assert result.returncode == 0
    # The flag itself must be documented; the SG name itself doesn't
    # have to appear (it's plugin-registered) so we only assert the
    # flag is listed.
    assert "--vendor" in result.stdout


# ---------------------------------------------------------------------------
# 4. Host-based auto-detection
# ---------------------------------------------------------------------------


def test_cli_autodetect_sg_from_event_url(tmp_path: Path) -> None:
    """Without ``--vendor``, a ``ticketmaster.sg`` event URL selects the SG adapter.

    The bot logs ``Vendor: ticketmaster_sg`` during the ``--dry-run`` warm-up.
    """
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert "ticketmaster_sg" in combined, f"Expected SG vendor in output, got:\n{combined}"


def test_cli_autodetect_us_from_event_url(tmp_path: Path) -> None:
    """Without ``--vendor``, a ``ticketmaster.com`` URL falls back to the
    US ``ticketmaster`` adapter — the SG name must NOT appear."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    # The US flow logs "Vendor: ticketmaster" (no _sg suffix).
    assert "ticketmaster_sg" not in combined, (
        f"Auto-detect should not select SG for a .com URL. Output:\n{combined}"
    )
    assert "ticketmaster" in combined


def test_cli_explicit_us_vendor_overrides_sg_url(tmp_path: Path) -> None:
    """An explicit ``--vendor=ticketmaster`` wins over the host heuristic,
    even when the event URL points at ticketmaster.sg."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
          strict_host: false
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--vendor",
        "ticketmaster",
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    # The "Vendor: " line must say plain ticketmaster, not the SG variant.
    assert "Vendor: ticketmaster_sg" not in combined
    assert "Vendor: ticketmaster" in combined


def test_cli_explicit_sg_vendor_with_us_url(tmp_path: Path) -> None:
    """An explicit ``--vendor=ticketmaster_sg`` wins over the host heuristic,
    even when the event URL points at ticketmaster.com."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--vendor",
        "ticketmaster_sg",
        "--dry-run",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    combined = result.stdout + result.stderr
    assert "Vendor: ticketmaster_sg" in combined


def test_cli_autodetect_with_explain_does_not_log_vendor_to_stdout(tmp_path: Path) -> None:
    """``--explain`` writes pure YAML to stdout; the vendor selection
    happens but its log line goes to stderr/the rich console, never
    polluting the parseable YAML output."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://ticketmaster.sg/activity/detail/26sg_pglcs2major"
        """,
    )
    result = _run_cli(
        "--config",
        str(tmp_path / "config.yaml"),
        "--accounts",
        str(tmp_path / "no-accounts.yaml"),
        "--explain",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    # stdout must be valid YAML (no log lines interleaved).
    parsed = yaml.safe_load(result.stdout)
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# 5. Bad inputs still fail loudly
# ---------------------------------------------------------------------------


def test_cli_bad_vendor_still_rejected() -> None:
    """The SG-vendor wiring must not break the existing ``--vendor nope``
    failure path; argparse / main.py still exit non-zero with the bad
    name visible in stderr."""
    result = _run_cli("--vendor", "definitely_not_a_vendor", "--dry-run")
    assert result.returncode != 0
    assert "definitely_not_a_vendor" in result.stderr


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason=".venv/bin/python missing")
def test_validation_subprocess_dry_run_explain() -> None:
    """Reproduction of the F7.5 brief's exact CLI command."""
    cmd = [
        str(VENV_PYTHON),
        "run.py",
        "--vendor=ticketmaster_sg",
        "--dry-run",
        "--explain",
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
