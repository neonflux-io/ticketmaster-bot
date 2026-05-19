"""SG-scoped selector helper.

The bundled US selector registry at :mod:`src.registry.selectors` is
loaded from ``config/selectors/ticketmaster.yaml`` and uses several
logical names (``login_email_input``, ``sold_out_marker``,
``not_on_sale_marker``, ``page_body``, ``main_content`` …) that would
collide with the SG YAML under :mod:`config/selectors/ticketmaster_sg.yaml`.

To keep selectors per-vendor and side-step those collisions, the SG
vendor modules consult this local helper instead of the shared registry.
The helper loads ``ticketmaster_sg.yaml`` once at import time, exposes
the same ``get`` / ``selector_for`` / ``locator`` surface area as the
shared registry, and is used by ``auth.py``, ``navigator.py`` and
``queue.py`` for every DOM lookup.

The strict YAML-only constraint still applies: no CSS / XPath strings
are baked into the SG Python modules, exactly as required by the
project AGENTS.md "no inline selectors in ``src/``" gate.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ...registry.base import NotRegistered, Registry

if TYPE_CHECKING:
    from playwright.async_api import FrameLocator, Locator, Page

log = logging.getLogger("ticketmaster-bot")

# Resolve the YAML relative to the repo root:
# ``src/vendors/ticketmaster_sg/selectors.py`` →
#   ``../../../config/selectors/ticketmaster_sg.yaml``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SG_YAML = _REPO_ROOT / "config" / "selectors" / "ticketmaster_sg.yaml"


_sg_registry: Registry[list[str]] = Registry("selectors[sg]")


def _load_yaml(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        raise FileNotFoundError(f"SG selector YAML not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"SG selector YAML {path} must be a top-level mapping")
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"SG selector YAML {path}: key {key!r} must be a non-empty string")
        if not isinstance(value, list) or not value:
            raise ValueError(
                f"SG selector YAML {path}: value for {key!r} must be a non-empty list of strings"
            )
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"SG selector YAML {path}: {key!r} contains a non-string or empty fallback"
                )
        out[key] = list(value)
    return out


def _load_default_yaml() -> None:
    """Populate the SG registry from the bundled YAML; idempotent."""
    try:
        loaded = _load_yaml(_SG_YAML)
    except FileNotFoundError:
        log.warning("SG selector YAML missing at %s; SG registry will be empty", _SG_YAML)
        return
    for name, fallbacks in loaded.items():
        if name in _sg_registry:
            continue
        _sg_registry.register(name, fallbacks)


_load_default_yaml()


def get(name: str) -> list[str]:
    """Return the SG fallback list registered under ``name``."""
    return _sg_registry.get(name)


def selector_for(name: str) -> str:
    """Comma-joined selector string for SG logical name ``name``."""
    return ", ".join(_sg_registry.get(name))


def try_selector_for(name: str) -> str | None:
    """Like :func:`selector_for` but returns ``None`` instead of raising."""
    try:
        return ", ".join(_sg_registry.get(name))
    except NotRegistered:
        return None


def locator(scope: Page | FrameLocator, name: str) -> Locator:
    """Resolve SG logical name ``name`` against ``scope`` and return ``.first``."""
    return scope.locator(selector_for(name)).first


def locator_multi(scope: Page | FrameLocator, name: str) -> Locator:
    """Like :func:`locator` but does not narrow to ``.first``."""
    return scope.locator(selector_for(name))


__all__ = [
    "get",
    "locator",
    "locator_multi",
    "selector_for",
    "try_selector_for",
]
