"""Per-context browser profile presets (desktop / mobile).

The Playwright ``Browser.new_context()`` / ``launch_persistent_context()``
APIs accept a single ``options`` mapping with keys like ``viewport``,
``is_mobile``, ``has_touch`` and ``user_agent``. These helpers populate
that mapping with sensible defaults for either a desktop browsing
session (the historic default) or a mobile/touch-driven session.

The two top-level entry points are :func:`apply_desktop_profile` and
:func:`apply_mobile_profile`. Both mutate the caller's ``options`` dict
**in place** and return it for convenience.

``apply_profile(name, options)`` dispatches by string name and is the
hook used by :mod:`src.vendors.ticketmaster.core` when reading the
``browser.profile`` config key.

Existing keys in the input dict are preserved for fields the caller
likely tuned themselves (``user_agent``, ``viewport``) while the
boolean fingerprint flags (``is_mobile`` / ``has_touch``) are
overwritten unconditionally because mixing them with the wrong viewport
produces a contradictory fingerprint that's easier to flag than either
profile alone.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

# Modern iPhone 14/15 chrome UA (matches Playwright's "iPhone 14" device
# preset). Kept literal so it round-trips through static analysis and so
# anyone grepping for "Mobile" in committed strings finds it.
MOBILE_USER_AGENT: str = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 "
    "Safari/604.1"
)

# iPhone 14 viewport - matches what real users see and tracks the
# feature spec (390 x 844).
MOBILE_VIEWPORT: dict[str, int] = {"width": 390, "height": 844}

# Desktop default matches the historic core.py launch (1366x900).
DESKTOP_VIEWPORT: dict[str, int] = {"width": 1366, "height": 900}

VALID_PROFILES: frozenset[str] = frozenset({"desktop", "mobile"})


def apply_desktop_profile(
    options: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Populate ``options`` with desktop defaults.

    The caller's existing ``viewport`` and ``user_agent`` keys are
    preserved (the desktop profile is historically the place where the
    jittered viewport / random Chrome UA lands), but ``is_mobile`` and
    ``has_touch`` are forced to ``False`` so the profile flags can't get
    out of sync with the rest of the context.

    Returns ``options`` for chaining; mutation is in place.
    """
    options.setdefault("viewport", dict(DESKTOP_VIEWPORT))
    options["is_mobile"] = False
    options["has_touch"] = False
    return options


def apply_mobile_profile(
    options: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Populate ``options`` with mobile/touch defaults.

    Unlike the desktop variant, the viewport is *always* overwritten to
    :data:`MOBILE_VIEWPORT` because the touch / coarse-pointer media
    features Playwright reports are sized off the viewport; a desktop
    viewport with ``is_mobile=True`` produces an obviously fake
    fingerprint that's worse than either profile.

    A caller-provided ``user_agent`` is preserved so power users can
    inject a custom mobile UA without losing the rest of the profile.

    Returns ``options`` for chaining; mutation is in place.
    """
    options["viewport"] = dict(MOBILE_VIEWPORT)
    options["is_mobile"] = True
    options["has_touch"] = True
    options.setdefault("user_agent", MOBILE_USER_AGENT)
    return options


def apply_profile(
    name: str,
    options: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Apply the profile identified by ``name`` to ``options``.

    ``name`` must be one of :data:`VALID_PROFILES` (``"desktop"`` or
    ``"mobile"``). Anything else is a configuration mistake and raises
    ``ValueError`` so the caller surfaces the bad config name instead of
    silently falling back to one of the presets.
    """
    if name == "desktop":
        return apply_desktop_profile(options)
    if name == "mobile":
        return apply_mobile_profile(options)
    raise ValueError(
        f"Unknown browser profile {name!r}. Valid options: {', '.join(sorted(VALID_PROFILES))}"
    )


__all__ = [
    "DESKTOP_VIEWPORT",
    "MOBILE_USER_AGENT",
    "MOBILE_VIEWPORT",
    "VALID_PROFILES",
    "apply_desktop_profile",
    "apply_mobile_profile",
    "apply_profile",
]
