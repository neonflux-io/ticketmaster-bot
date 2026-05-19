"""Selector registry - logical name → list of CSS/XPath fallback selectors.

The registry is populated from ``config/selectors/*.yaml`` (currently only
``ticketmaster.yaml``) at import time so every vendor / strategy module can
simply call ``from src.registry.selectors import locator`` and resolve
logical names like ``"add_to_cart_button"`` to a real Playwright Locator.

The combined selector string returned by :func:`selector_for` joins every
fallback with a literal ``", "``; Playwright treats the comma as a CSS-or
operator at parse time, so the first match in document order wins. Helpers
:func:`locator` / :func:`locator_multi` wrap that string and call
``.first`` (single) or return the multi-match Locator (multi).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .base import Registry

if TYPE_CHECKING:
    from playwright.async_api import FrameLocator, Locator, Page

log = logging.getLogger("ticketmaster-bot")

# Resolve the YAML file relative to the repo root (this file lives at
# ``src/registry/selectors.py`` → ``../../config/selectors/ticketmaster.yaml``).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_YAML = _REPO_ROOT / "config" / "selectors" / "ticketmaster.yaml"


# The selector registry stores a list of CSS/XPath fallback strings per
# logical name. Storing the raw list (instead of a pre-joined string) lets
# callers introspect individual fallbacks - useful for logging on failure.
registry: Registry[list[str]] = Registry(
    "selectors", entry_point_group="ticketmaster_bot.selectors"
)


def register(name: str, obj: list[str]) -> list[str]:
    """Register a list of fallback selectors under ``name``."""
    return registry.register(name, obj)


def get(name: str) -> list[str]:
    """Return the fallback list registered under ``name``."""
    return registry.get(name)


def all() -> dict[str, list[str]]:
    """Return a snapshot of every registered selector entry."""
    return registry.all()


def selector_for(name: str) -> str:
    """Return the comma-joined selector string for ``name``.

    The fallback list is joined with ``", "`` so Playwright treats it as a
    CSS-or selector (first match in document order wins).
    """
    return ", ".join(registry.get(name))


def locator(scope: Page | FrameLocator, name: str) -> Locator:
    """Resolve ``name`` against ``scope`` and return ``locator.first``.

    ``scope`` may be either a :class:`playwright.async_api.Page` or a
    :class:`playwright.async_api.FrameLocator`; both expose a ``locator``
    method with the same signature.
    """
    return scope.locator(selector_for(name)).first


def locator_multi(scope: Page | FrameLocator, name: str) -> Locator:
    """Like :func:`locator` but does not narrow to ``.first``."""
    return scope.locator(selector_for(name))


def _load_yaml(path: Path) -> dict[str, list[str]]:
    """Load and validate a selector YAML file into ``{name: [selector, ...]}``."""
    if not path.is_file():
        raise FileNotFoundError(f"Selector YAML not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Selector YAML {path} must be a top-level mapping")
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"Selector YAML {path}: key {key!r} must be a non-empty string")
        if not isinstance(value, list) or not value:
            raise ValueError(
                f"Selector YAML {path}: value for {key!r} must be a non-empty list of strings"
            )
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"Selector YAML {path}: {key!r} contains a non-string or empty fallback"
                )
        out[key] = list(value)
    return out


def _load_default_yaml() -> None:
    """Populate the singleton registry from the bundled ticketmaster.yaml.

    The call is a no-op if the registry already contains the entry for a
    given name (so re-importing in tests / subprocess invocations is safe).
    """
    try:
        loaded = _load_yaml(_DEFAULT_YAML)
    except FileNotFoundError:
        log.warning("Selector YAML missing at %s; registry will be empty", _DEFAULT_YAML)
        return
    for name, fallbacks in loaded.items():
        if name in registry:
            continue
        registry.register(name, fallbacks)


# Side-effect on import: load the bundled YAML once. This is the contract
# the F1.3 feature ("loads the YAML on import") relies on.
_load_default_yaml()


__all__ = [
    "all",
    "get",
    "locator",
    "locator_multi",
    "register",
    "registry",
    "selector_for",
]
