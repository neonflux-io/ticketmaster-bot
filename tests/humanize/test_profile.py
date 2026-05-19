"""Tests for ``src.humanize.profile`` driven by real Chromium contexts.

``apply_mobile_profile`` must produce a context-options dict that, when
fed to ``Browser.new_context``, yields a browsing context where
``navigator.maxTouchPoints > 0`` and ``matchMedia('(pointer: coarse)')``
matches. ``apply_desktop_profile`` is the historic default — touch-free
and pointer-fine.

The shared session-scoped ``chromium`` browser fixture from
``tests/conftest.py`` is used to launch fresh contexts (one per test so
no profile leaks between tests).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from playwright.async_api import Browser, BrowserContext

from src.humanize.profile import (
    DESKTOP_VIEWPORT,
    MOBILE_USER_AGENT,
    MOBILE_VIEWPORT,
    VALID_PROFILES,
    apply_desktop_profile,
    apply_mobile_profile,
    apply_profile,
)


@pytest_asyncio.fixture
async def mobile_context(chromium: Browser) -> AsyncIterator[BrowserContext]:
    """Fresh BrowserContext with the mobile profile applied."""
    options: dict[str, object] = {}
    apply_mobile_profile(options)
    context = await chromium.new_context(**options)
    try:
        yield context
    finally:
        await context.close()


@pytest_asyncio.fixture
async def desktop_context(chromium: Browser) -> AsyncIterator[BrowserContext]:
    """Fresh BrowserContext with the desktop profile applied."""
    options: dict[str, object] = {}
    apply_desktop_profile(options)
    context = await chromium.new_context(**options)
    try:
        yield context
    finally:
        await context.close()


# ---------------------------------------------------------------------------
# [dom.humanize-profile-mobile]
# ---------------------------------------------------------------------------


async def test_mobile_profile_has_coarse_pointer(
    mobile_context: BrowserContext,
) -> None:
    """``matchMedia('(pointer: coarse)').matches`` must be True."""
    page = await mobile_context.new_page()
    await page.goto("about:blank")

    coarse = await page.evaluate("() => window.matchMedia('(pointer: coarse)').matches")
    assert coarse is True


async def test_mobile_profile_has_touch_points(
    mobile_context: BrowserContext,
) -> None:
    """``navigator.maxTouchPoints`` must be > 0 in a mobile context."""
    page = await mobile_context.new_page()
    await page.goto("about:blank")

    max_touch = await page.evaluate("() => navigator.maxTouchPoints")
    assert isinstance(max_touch, int)
    assert max_touch > 0, f"expected mobile maxTouchPoints > 0, got {max_touch}"


async def test_mobile_profile_uses_390x844_viewport(
    mobile_context: BrowserContext,
) -> None:
    """Mobile viewport must be the iPhone-sized 390x844 rectangle.

    In ``is_mobile=True`` mode Playwright's layout viewport
    (``window.innerWidth``) defers to the page's ``<meta name="viewport">``
    tag; we therefore inject ``width=device-width`` so the layout
    viewport matches the configured device viewport.
    """
    page = await mobile_context.new_page()
    await page.set_content(
        '<!doctype html><html><head><meta name="viewport" '
        'content="width=device-width, initial-scale=1"></head>'
        "<body>device-width</body></html>"
    )

    width = await page.evaluate("() => window.innerWidth")
    height = await page.evaluate("() => window.innerHeight")
    assert width == 390, f"expected width 390, got {width}"
    assert height == 844, f"expected height 844, got {height}"

    # window.screen.{width,height} reports the device viewport and is
    # independent of the page's <meta viewport> tag, so it also pins the
    # configuration regardless of page content.
    screen_w = await page.evaluate("() => window.screen.width")
    screen_h = await page.evaluate("() => window.screen.height")
    assert screen_w == 390, f"screen width should be 390, got {screen_w}"
    assert screen_h == 844, f"screen height should be 844, got {screen_h}"


async def test_mobile_profile_sets_mobile_user_agent(
    mobile_context: BrowserContext,
) -> None:
    page = await mobile_context.new_page()
    await page.goto("about:blank")
    ua: str = await page.evaluate("() => navigator.userAgent")
    assert "Mobile" in ua or "iPhone" in ua, f"unexpected mobile UA: {ua!r}"


# ---------------------------------------------------------------------------
# Desktop profile - confirm it stays the historic default.
# ---------------------------------------------------------------------------


async def test_desktop_profile_no_touch_points(
    desktop_context: BrowserContext,
) -> None:
    """Desktop context must report zero touch points and fine pointer."""
    page = await desktop_context.new_page()
    await page.goto("about:blank")

    max_touch = await page.evaluate("() => navigator.maxTouchPoints")
    assert max_touch == 0, f"desktop maxTouchPoints should be 0, got {max_touch}"

    coarse = await page.evaluate("() => window.matchMedia('(pointer: coarse)').matches")
    assert coarse is False


# ---------------------------------------------------------------------------
# apply_* helpers - pure dict-manipulation checks (no Playwright needed).
# ---------------------------------------------------------------------------


def test_apply_mobile_profile_returns_dict_with_required_keys() -> None:
    opts: dict[str, object] = {}
    result = apply_mobile_profile(opts)
    assert result is opts  # mutates in place
    assert opts["viewport"] == MOBILE_VIEWPORT
    assert opts["is_mobile"] is True
    assert opts["has_touch"] is True
    assert opts["user_agent"] == MOBILE_USER_AGENT


def test_apply_mobile_profile_preserves_explicit_user_agent() -> None:
    """An existing ``user_agent`` key must win over the mobile default."""
    opts: dict[str, object] = {"user_agent": "CustomUA/1.0"}
    apply_mobile_profile(opts)
    assert opts["user_agent"] == "CustomUA/1.0"
    # But viewport / flags are unconditionally overwritten.
    assert opts["viewport"] == MOBILE_VIEWPORT
    assert opts["is_mobile"] is True


def test_apply_desktop_profile_sets_desktop_defaults() -> None:
    opts: dict[str, object] = {}
    result = apply_desktop_profile(opts)
    assert result is opts
    assert opts["viewport"] == DESKTOP_VIEWPORT
    assert opts["is_mobile"] is False
    assert opts["has_touch"] is False


def test_apply_desktop_profile_preserves_existing_viewport() -> None:
    """A caller-set viewport (e.g. the jittered one) must not be clobbered."""
    opts: dict[str, object] = {"viewport": {"width": 1400, "height": 920}}
    apply_desktop_profile(opts)
    assert opts["viewport"] == {"width": 1400, "height": 920}


def test_apply_profile_dispatches_by_name() -> None:
    mobile_opts: dict[str, object] = {}
    apply_profile("mobile", mobile_opts)
    assert mobile_opts["is_mobile"] is True

    desktop_opts: dict[str, object] = {}
    apply_profile("desktop", desktop_opts)
    assert desktop_opts["is_mobile"] is False


def test_apply_profile_rejects_unknown_name() -> None:
    with pytest.raises(ValueError) as info:
        apply_profile("tablet", {})
    assert "tablet" in str(info.value)


def test_valid_profiles_exposes_known_set() -> None:
    assert VALID_PROFILES == frozenset({"desktop", "mobile"})


# ---------------------------------------------------------------------------
# Config integration.
# ---------------------------------------------------------------------------


def test_config_browser_profile_defaults_to_desktop(tmp_path) -> None:
    """``browser.profile`` defaults to ``"desktop"`` when omitted."""
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """
    )

    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.browser.profile == "desktop"


def test_config_browser_profile_accepts_mobile(tmp_path) -> None:
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        browser:
          profile: mobile
        """
    )

    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.browser.profile == "mobile"


def test_config_browser_profile_rejects_unknown(tmp_path) -> None:
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        browser:
          profile: tablet
        """
    )

    with pytest.raises(ValueError) as info:
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert "tablet" in str(info.value)
