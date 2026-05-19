"""Command-line interface for the Ticketmaster bot.

This module owns the full argparse surface (extracted from ``src/main.py``
in feature F1.5) along with the pure-Python helpers used by the parser.
Keeping every flag definition in one place makes the CLI easy to unit
test and lets ``--set`` / ``--profile`` / ``--events`` overrides be
unit-checked without spawning a subprocess.

Supported flags
---------------

Existing (preserved verbatim):

- ``--config / -c``      Path to the main config YAML.
- ``--accounts / -a``    Path to the accounts YAML.
- ``--account-name``     Run only the account with this ``name``.
- ``--headless`` / ``--no-headless``           Force browser headlessness.
- ``--auto-purchase`` / ``--no-auto-purchase`` Force ``checkout.auto_purchase``.
- ``--dry-run``          Validate config and exit before launching a browser.

New in F1.5:

- ``--profile``          Overlay ``config/profiles/<name>.yaml``.
- ``--set KEY.PATH=VAL`` Override a dotted-path config value (repeatable).
- ``--events URL``       Override the ``events:`` list (repeatable, also comma
                         splittable). Wins over the file's ``event/events`` keys.
- ``--vendor``           Vendor adapter name (default ``ticketmaster``).
                         Validated against :mod:`src.registry.vendors`.
- ``--parallel``         Enable parallel multi-account orchestration.
- ``--max-parallel N``   Maximum concurrent account runners (positive int).
- ``--stagger SECONDS``  Stagger account launches by N seconds (non-neg float).
- ``--explain``          Print the fully merged resolved config as YAML and exit.

Error contract
--------------

- Invalid ``--vendor`` value → non-zero exit; the offending name appears in stderr.
- Invalid ``--config`` path (file not found) → exit code 2.
- Invalid ``--profile`` name (profile YAML not found) → exit code 2.
- Invalid ``--max-parallel`` (non-positive) or ``--stagger`` (negative) → exit code 2 via argparse.
- ``--headless``/``--no-headless`` and ``--auto-purchase``/``--no-auto-purchase``
  are each mutually exclusive (argparse-enforced).
"""

from __future__ import annotations

import argparse
import copy
from typing import Any

_TRUE_LITERALS = {"true", "yes", "on"}
_FALSE_LITERALS = {"false", "no", "off"}
_NULL_LITERALS = {"null", "none", "~"}


# ---------------------------------------------------------------------------
# --set parsing helpers.
# ---------------------------------------------------------------------------


def _coerce_scalar(text: str) -> Any:
    """Convert a CLI string value into the most plausible scalar type.

    Recognised forms (in order of precedence): booleans, ``null``,
    integers, floats, and finally the raw string. This matches the
    behaviour users expect from ``--set tickets.quantity=4`` (integer)
    and ``--set checkout.auto_purchase=false`` (boolean).
    """
    lowered = text.lower()
    if lowered in _TRUE_LITERALS:
        return True
    if lowered in _FALSE_LITERALS:
        return False
    if lowered in _NULL_LITERALS:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _assign_dotted(target: dict[str, Any], path: str, value: Any) -> None:
    """Assign ``value`` into ``target`` at the dotted ``path``.

    Intermediate keys that don't exist (or whose existing value is not a
    dict) are created as empty dicts. The final segment receives ``value``.
    """
    if not path:
        raise ValueError("--set: dotted path may not be empty")
    parts = path.split(".")
    for part in parts:
        if not part:
            raise ValueError(f"--set: empty segment in dotted path {path!r}")
    cursor = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = value


def parse_set_overrides(items: list[str]) -> dict[str, Any]:
    """Parse ``--set key.path=value`` items into a nested override dict.

    Examples:

    >>> parse_set_overrides(["tickets.quantity=4"])
    {'tickets': {'quantity': 4}}
    >>> parse_set_overrides(["checkout.auto_purchase=false"])
    {'checkout': {'auto_purchase': False}}

    Raises
    ------
    ValueError
        If any item is missing the ``=`` separator or has an empty key.
    """
    result: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(
                f"--set expects key.path=value, got {item!r} (no '=' separator)"
            )
        key, _, raw_value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--set: empty key in {item!r}")
        # The value side keeps its original whitespace - users may legitimately
        # want trailing spaces in strings - but we strip leading space the same
        # way a shell would have on flag parsing.
        value = _coerce_scalar(raw_value.strip())
        _assign_dotted(result, key, value)
    return copy.deepcopy(result)


def parse_events_flag(items: list[str]) -> list[str]:
    """Flatten a list of ``--events`` flag values into individual URLs.

    Each ``--events`` value may be a single URL or a comma-separated list
    of URLs. Empty values are dropped. Order is preserved.
    """
    urls: list[str] = []
    for item in items:
        for chunk in item.split(","):
            chunk = chunk.strip()
            if chunk:
                urls.append(chunk)
    return urls


# ---------------------------------------------------------------------------
# argparse type validators.
# ---------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {text!r}"
        ) from exc
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be >= 1, got {value}"
        )
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a non-negative number, got {text!r}"
        ) from exc
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"must be >= 0, got {value}"
        )
    return value


# ---------------------------------------------------------------------------
# Parser construction.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the fully-configured top-level argparse parser.

    Factored out so tests can build the parser and inspect its namespace
    without spawning a subprocess.
    """
    p = argparse.ArgumentParser(
        prog="ticketmaster-bot",
        description="Ticketmaster ticket-buying bot",
    )

    # --- I/O paths --------------------------------------------------------
    p.add_argument(
        "--config",
        "-c",
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)",
    )
    p.add_argument(
        "--accounts",
        "-a",
        default="config/accounts.yaml",
        help="Path to accounts file (default: config/accounts.yaml)",
    )
    p.add_argument(
        "--account-name",
        default=None,
        help="Run only the account with this name (default: first account)",
    )

    # --- Vendor selection -------------------------------------------------
    p.add_argument(
        "--vendor",
        default="ticketmaster",
        help=(
            "Vendor adapter to use (default: ticketmaster). "
            "Must be registered with src.registry.vendors."
        ),
    )

    # --- Layered config overlays -----------------------------------------
    p.add_argument(
        "--profile",
        default=None,
        help=(
            "Apply config/profiles/<name>.yaml on top of the main config. "
            "Ships with 'fast' and 'safe'."
        ),
    )
    p.add_argument(
        "--set",
        dest="set_overrides",
        action="append",
        default=[],
        metavar="KEY.PATH=VALUE",
        help=(
            "Override a config value at a dotted path. "
            "Repeatable, e.g. --set tickets.quantity=4 --set checkout.auto_purchase=false"
        ),
    )
    p.add_argument(
        "--events",
        dest="events",
        action="append",
        default=[],
        metavar="URL",
        help=(
            "Override the events list with one or more URLs. "
            "Repeatable; each value may be a single URL or a comma-separated list."
        ),
    )

    # --- Parallel orchestration -----------------------------------------
    p.add_argument(
        "--parallel",
        action="store_true",
        help="Enable parallel multi-account orchestration.",
    )
    p.add_argument(
        "--max-parallel",
        dest="max_parallel",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Maximum number of accounts to run concurrently (positive integer).",
    )
    p.add_argument(
        "--stagger",
        dest="stagger_seconds",
        type=_non_negative_float,
        default=None,
        metavar="SECONDS",
        help="Stagger account launches by this many seconds (non-negative float).",
    )

    # --- Browser / checkout toggles -------------------------------------
    headless_group = p.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=None,
        help="Force headless mode regardless of config",
    )
    headless_group.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Force a visible browser regardless of config",
    )

    purchase_group = p.add_mutually_exclusive_group()
    purchase_group.add_argument(
        "--auto-purchase",
        dest="auto_purchase",
        action="store_true",
        default=None,
        help="Force auto_purchase=true regardless of config (USE WITH CAUTION)",
    )
    purchase_group.add_argument(
        "--no-auto-purchase",
        dest="auto_purchase",
        action="store_false",
        help="Force auto_purchase=false regardless of config",
    )

    # --- Run-mode toggles ------------------------------------------------
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and exit without launching a browser",
    )
    p.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Dump the fully resolved config as YAML to stdout and exit. "
            "Useful for verifying profile/--set overlays before a real run."
        ),
    )
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``argv`` (or ``sys.argv[1:]`` when None) into a Namespace."""
    return build_parser().parse_args(argv)


__all__ = [
    "build_parser",
    "parse_args",
    "parse_events_flag",
    "parse_set_overrides",
]
