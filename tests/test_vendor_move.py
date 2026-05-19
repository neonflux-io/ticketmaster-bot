"""Tests for F1.2 vendor move + backwards-compat shim.

These tests prove:
- The Ticketmaster vendor code now lives under ``src/vendors/ticketmaster/``.
- Each of ``auth.py``, ``cart.py``, ``checkout.py``, ``core.py``,
  ``navigator.py``, ``queue.py`` is a real, non-trivial file.
- A ``VendorAdapter`` ABC exists under ``src/vendors/base.py``.
- A concrete ``TicketmasterAdapter`` wires the moved modules into the ABC
  and is registered in :mod:`src.registry.vendors` under the name
  ``ticketmaster``.
- Legacy ``from src.bot import auth, cart, checkout, navigator, queue`` and
  ``from src.bot.core import BotRunner`` continue to resolve after the move,
  via re-export shims under ``src/bot/``.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


VENDOR_MODULES = ("auth", "cart", "checkout", "core", "navigator", "queue")


def test_vendor_directory_layout() -> None:
    """All six vendor modules exist under src/vendors/ticketmaster/ as real files."""
    base_dir = REPO_ROOT / "src" / "vendors" / "ticketmaster"
    assert base_dir.is_dir(), f"expected {base_dir} to exist"
    for name in VENDOR_MODULES:
        path = base_dir / f"{name}.py"
        assert path.is_file(), f"missing {path}"
        # validate non-trivial: >200 bytes per validation contract
        size = path.stat().st_size
        assert size > 200, f"{path} too small ({size} bytes)"


def test_vendor_modules_importable() -> None:
    """All vendor submodules import cleanly under src.vendors.ticketmaster."""
    for name in VENDOR_MODULES:
        mod = importlib.import_module(f"src.vendors.ticketmaster.{name}")
        assert mod is not None


def test_vendor_adapter_abc_exists() -> None:
    """src.vendors.base defines an abstract VendorAdapter base class."""
    base_mod = importlib.import_module("src.vendors.base")
    cls = getattr(base_mod, "VendorAdapter", None)
    assert cls is not None, "VendorAdapter not found in src.vendors.base"
    # ABC: cannot be directly instantiated
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]


def test_ticketmaster_adapter_subclasses_base() -> None:
    """TicketmasterAdapter inherits from VendorAdapter and exposes the moved modules."""
    base_mod = importlib.import_module("src.vendors.base")
    adapter_mod = importlib.import_module("src.vendors.ticketmaster.adapter")
    assert issubclass(adapter_mod.TicketmasterAdapter, base_mod.VendorAdapter)
    # Adapter instances should expose the per-step modules so flow code can
    # call e.g. adapter.auth.login(...).
    adapter = adapter_mod.TicketmasterAdapter()
    for name in VENDOR_MODULES:
        attr = getattr(adapter, name, None)
        assert attr is not None, f"TicketmasterAdapter missing attribute {name!r}"


def test_ticketmaster_adapter_registered() -> None:
    """The vendor registry knows about 'ticketmaster' after import."""
    from src.registry import vendors as vendor_registry

    importlib.import_module("src.vendors.ticketmaster")
    obj = vendor_registry.get("ticketmaster")
    adapter_mod = importlib.import_module("src.vendors.ticketmaster.adapter")
    assert obj is adapter_mod.TicketmasterAdapter


def test_bot_shim_resolves_botrunner_subprocess() -> None:
    """Validation-contract command: from src.bot.core import BotRunner; plus per-module shims."""
    cmd = [
        str(VENV_PYTHON),
        "-c",
        (
            "from src.bot.core import BotRunner; "
            "from src.bot import auth, cart, checkout, navigator, queue; "
            "print('OK')"
        ),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"shim import failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "OK"


def test_bot_shim_attributes_match_vendor_modules() -> None:
    """The src.bot.* shim modules re-export the names from src.vendors.ticketmaster.*"""
    from src.bot import auth as shim_auth
    from src.bot import cart as shim_cart
    from src.bot import checkout as shim_checkout
    from src.bot import navigator as shim_nav
    from src.bot import queue as shim_queue
    from src.bot.core import BotRunner as ShimBotRunner
    from src.vendors.ticketmaster import auth as v_auth
    from src.vendors.ticketmaster import cart as v_cart
    from src.vendors.ticketmaster import checkout as v_checkout
    from src.vendors.ticketmaster import navigator as v_nav
    from src.vendors.ticketmaster import queue as v_queue
    from src.vendors.ticketmaster.core import BotRunner as VendorBotRunner

    # The shim modules should expose the same callable objects so existing
    # code that imports e.g. ``from src.bot.auth import login`` keeps working
    # with the same object identity.
    assert shim_auth.login is v_auth.login
    assert shim_auth.AuthError is v_auth.AuthError
    assert shim_cart.add_to_cart is v_cart.add_to_cart
    assert shim_checkout.run_checkout is v_checkout.run_checkout
    assert shim_nav.detect_state is v_nav.detect_state
    assert shim_nav.NavigationError is v_nav.NavigationError
    assert shim_queue.wait_through_queue is v_queue.wait_through_queue
    assert ShimBotRunner is VendorBotRunner


def test_no_orphan_definitions_left_in_src_bot() -> None:
    """src/bot/*.py shim modules should not redefine the vendor code.

    They should be thin re-export wrappers. Concretely: each shim file should
    be small (< 1 KB) and import from src.vendors.ticketmaster.
    """
    shim_dir = REPO_ROOT / "src" / "bot"
    for name in VENDOR_MODULES:
        path = shim_dir / f"{name}.py"
        assert path.is_file(), f"missing shim file {path}"
        contents = path.read_text(encoding="utf-8")
        assert "src.vendors.ticketmaster" in contents, (
            f"{path} does not re-export from src.vendors.ticketmaster"
        )
        # The shim should be small - real implementation has moved.
        assert len(contents) < 1024, (
            f"{path} is too large ({len(contents)} bytes) - implementation "
            "should live under src/vendors/ticketmaster/"
        )


def test_main_module_still_loads() -> None:
    """src.main imports BotRunner from the legacy path; that path must still resolve."""
    # Re-importing is fine - exec a subprocess to avoid polluting this test
    # process with main()'s side effects.
    cmd = [
        str(VENV_PYTHON),
        "-c",
        "import importlib; importlib.import_module('src.main'); print('OK')",
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"src.main import failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in result.stdout


def test_cli_dry_run_still_works() -> None:
    """`run.py --dry-run` must still exit 0 after the vendor move."""
    cmd = [str(VENV_PYTHON), "run.py", "--dry-run"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"--dry-run failed:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


# Sanity guard: VENV_PYTHON must exist (mission readiness verified this).
def _venv_python_exists() -> bool:
    return VENV_PYTHON.exists()


@pytest.mark.skipif(not _venv_python_exists(), reason=".venv/bin/python missing")
def test_validation_contract_command_exact() -> None:
    """Exact reproduction of the [refactor.bot-shim] assertion command."""
    cmd = [
        str(VENV_PYTHON),
        "-c",
        (
            "from src.bot.core import BotRunner; "
            "from src.bot import auth, cart, checkout, navigator, queue; "
            "print('OK')"
        ),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip().endswith("OK")


# Touch sys to avoid unused-import in some linters.
_ = sys
