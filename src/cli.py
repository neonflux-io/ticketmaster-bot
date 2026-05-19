"""CLI helpers shared by ``src/main.py`` and tests.

The full argparse surface still lives in :mod:`src.main` for now; this
module owns the pure parsing helpers (``--set`` resolution, value type
inference) so they can be unit-tested without spawning a subprocess.

F1.5 will move the rest of argparse here; this file is the staging ground.
"""

from __future__ import annotations

import copy
from typing import Any

_TRUE_LITERALS = {"true", "yes", "on"}
_FALSE_LITERALS = {"false", "no", "off"}
_NULL_LITERALS = {"null", "none", "~"}


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


__all__ = ["parse_set_overrides"]
