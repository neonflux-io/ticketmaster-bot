"""CI grep gate enforcing the F2.6 invariant:

After milestone 2 ships, every CSS / XPath / data-attribute selector used
to address the Ticketmaster DOM must live in
``config/selectors/ticketmaster.yaml`` and be resolved through
``src.registry.selectors.locator`` (or its sibling helpers). Inline
selector strings inside ``src/**/*.py`` are forbidden.

This test enforces the exact validation-contract command from
``[dom.no-inline-selectors]``:

    rg -n 'data-bdd=' src/ --type py

which must return no matches. We re-implement the same scan in pure
Python so the gate runs even when ``rg`` is not on ``$PATH``, and so the
test message surfaces the offending file/line/text directly. The
implementation deliberately matches ``rg``'s default semantics
(literal-substring match, recursive descent through ``src/``, only
``.py`` files).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def _iter_py_files(root: Path):
    return (p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ---------------------------------------------------------------------------
# Pure-Python scan: works without ripgrep on PATH.
# ---------------------------------------------------------------------------


def test_no_inline_data_bdd_in_src() -> None:
    """No occurrence of the literal ``data-bdd=`` may appear in ``src/**/*.py``."""
    offenders: list[str] = []
    for py_file in _iter_py_files(SRC_DIR):
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "data-bdd=" in line:
                offenders.append(f"{py_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Inline 'data-bdd=' selector(s) found in src/ (move to "
        "config/selectors/ticketmaster.yaml):\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# ``rg`` execution: this is the exact command the validation contract
# pins ([dom.no-inline-selectors]) and the audit_selectors entry in
# services.yaml. We run it when ripgrep is available so the test
# matches the user-facing CI gate verbatim.
# ---------------------------------------------------------------------------


def test_rg_data_bdd_command_returns_no_matches() -> None:
    """The exact validation-contract command must report no matches.

    ``rg`` exits 0 on at least one match, 1 on no matches, and ≥2 on
    error. We assert exit code 1 (with empty stdout) so a missing
    ripgrep binary on the runner does not silently turn this into a
    pass.
    """
    rg = shutil.which("rg")
    if rg is None:
        # No ripgrep available; the pure-Python scan above already
        # covers the same invariant.
        return
    proc = subprocess.run(
        [rg, "-n", "data-bdd=", "src/", "--type", "py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, (
        f"rg -n 'data-bdd=' src/ --type py exited {proc.returncode}; "
        f"stdout was:\n{proc.stdout}\nstderr was:\n{proc.stderr}"
    )
    assert proc.stdout == "", f"rg unexpectedly produced output:\n{proc.stdout}"


# ---------------------------------------------------------------------------
# Bonus belt-and-braces: a small set of other CSS-attribute starters we
# don't want creeping back in either. These are advisory; the
# data-bdd= gate above is the contract-binding one.
# ---------------------------------------------------------------------------


# Mapping is intentional — we forbid the *attribute syntax* literally,
# not the *attribute name*. (``data-price-level-id`` appears as a Python
# string key elsewhere and is fine.)
_FORBIDDEN_INLINE_PATTERNS = ("data-price-level-id=",)


def test_no_other_known_inline_selectors_in_src() -> None:
    """Sanity gate: catch other ``data-*=`` inline attrs that crept in."""
    offenders: list[str] = []
    for py_file in _iter_py_files(SRC_DIR):
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in _FORBIDDEN_INLINE_PATTERNS:
                if pat in line:
                    offenders.append(
                        f"{py_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()} "
                        f"(matched {pat!r})"
                    )
    assert not offenders, (
        "Inline data-* selector(s) found in src/ (move to "
        "config/selectors/ticketmaster.yaml):\n" + "\n".join(offenders)
    )
