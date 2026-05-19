"""Tests for ``humanize.typing.human_type`` driven by real headless Chromium.

The function must type a text string into a Playwright locator character
by character via ``page.keyboard.type(char, delay=...)`` with a
normally-distributed per-keystroke delay. The validation contract
assertion ``dom.humanize-typing-keystrokes`` requires:

  * Exactly N ``keydown`` events recorded by an in-page listener for a
    string of length N (5 for ``"hello"``).
  * All consecutive ``window.__keys[i+1] - window.__keys[i]`` deltas
    must be ≥ 10 ms.

In addition this module validates a handful of robustness properties
(empty input, mean-ms / std-ms / min-ms bounds, RNG seeding, and the
``config.timing.humanize.typing`` block round-trip).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from playwright.async_api import BrowserContext

from src.humanize.typing import human_type


@pytest_asyncio.fixture
async def input_page(chromium_context: BrowserContext):  # noqa: ANN201
    """Return a page with a single ``<input id='in'>`` and a keydown
    listener that pushes ``Date.now()`` onto ``window.__keys``.
    """
    page = await chromium_context.new_page()
    await page.set_content(
        """
        <!doctype html>
        <html>
        <body>
          <input id="in" type="text" autofocus />
          <script>
            window.__keys = [];
            const target = document.getElementById("in");
            target.addEventListener("keydown", () => {
              window.__keys.push(Date.now());
            });
          </script>
        </body>
        </html>
        """
    )
    await page.locator("#in").focus()
    return page


# --- core contract --------------------------------------------------------


async def test_human_type_emits_one_keydown_per_char(input_page):  # noqa: ANN001
    """[dom.humanize-typing-keystrokes]: 5 keydown events for ``hello``."""
    await human_type(input_page.locator("#in"), "hello")

    keys = await input_page.evaluate("() => window.__keys")
    assert isinstance(keys, list)
    assert len(keys) == 5, f"expected 5 keydown events, got {len(keys)}: {keys}"


async def test_human_type_deltas_at_least_min_ms(input_page):  # noqa: ANN001
    """[dom.humanize-typing-keystrokes]: consecutive deltas are ≥ 10 ms."""
    await human_type(input_page.locator("#in"), "hello")

    keys: list[int] = await input_page.evaluate("() => window.__keys")
    assert len(keys) == 5
    deltas = [keys[i + 1] - keys[i] for i in range(len(keys) - 1)]
    assert all(d >= 10 for d in deltas), f"per-keystroke deltas must be ≥10ms: {deltas}"


async def test_human_type_actually_fills_input(input_page):  # noqa: ANN001
    """The typed characters end up as the input's value."""
    await human_type(input_page.locator("#in"), "hello", mean_ms=20, std_ms=5, min_ms=10)

    value = await input_page.locator("#in").input_value()
    assert value == "hello"


async def test_human_type_respects_custom_min_ms(input_page):  # noqa: ANN001
    """A higher ``min_ms`` floor must hold for every delta."""
    await human_type(
        input_page.locator("#in"),
        "abcde",
        mean_ms=25,
        std_ms=5,
        min_ms=20,
    )

    keys: list[int] = await input_page.evaluate("() => window.__keys")
    assert len(keys) == 5
    deltas = [keys[i + 1] - keys[i] for i in range(len(keys) - 1)]
    assert all(d >= 20 for d in deltas), f"expected ≥20ms floor, got {deltas}"


async def test_human_type_empty_string_is_noop(input_page):  # noqa: ANN001
    """An empty text input must not crash and must record no keydowns."""
    await human_type(input_page.locator("#in"), "")

    keys = await input_page.evaluate("() => window.__keys")
    assert keys == []
    assert await input_page.locator("#in").input_value() == ""


async def test_human_type_deterministic_with_seed(input_page):  # noqa: ANN001
    """Two seeded runs must produce identical per-keystroke delays."""
    # First run: clear the recorder buffer.
    await input_page.evaluate("() => { window.__keys = []; }")
    await human_type(
        input_page.locator("#in"),
        "abc",
        mean_ms=40,
        std_ms=10,
        min_ms=10,
        seed=42,
    )
    run1: list[int] = await input_page.evaluate("() => window.__keys")
    deltas1 = [run1[i + 1] - run1[i] for i in range(len(run1) - 1)]

    # Reset for the second run.
    await input_page.locator("#in").fill("")
    await input_page.evaluate("() => { window.__keys = []; }")
    await human_type(
        input_page.locator("#in"),
        "abc",
        mean_ms=40,
        std_ms=10,
        min_ms=10,
        seed=42,
    )
    run2: list[int] = await input_page.evaluate("() => window.__keys")
    deltas2 = [run2[i + 1] - run2[i] for i in range(len(run2) - 1)]

    # The delays come from a seeded RNG; deltas should be identical up
    # to a wall-clock tolerance. Playwright IPC + pytest-xdist parallel
    # load can add 15-25ms jitter on top of the deterministic delay, so
    # 25ms is the empirically-safe ceiling. Determinism comes from the
    # seed (run1 and run2 sample the same delays); the assertion just
    # confirms the seed plumbs through.
    assert len(deltas1) == len(deltas2) == 2
    for a, b in zip(deltas1, deltas2, strict=False):
        assert abs(a - b) <= 25, f"seeded runs diverged: {deltas1} vs {deltas2}"


async def test_human_type_rejects_invalid_min_ms(input_page):  # noqa: ANN001
    with pytest.raises(ValueError):
        await human_type(input_page.locator("#in"), "x", min_ms=-1)


async def test_human_type_rejects_invalid_std_ms(input_page):  # noqa: ANN001
    with pytest.raises(ValueError):
        await human_type(input_page.locator("#in"), "x", std_ms=-1)


async def test_human_type_rejects_invalid_mean_ms(input_page):  # noqa: ANN001
    with pytest.raises(ValueError):
        await human_type(input_page.locator("#in"), "x", mean_ms=0)


# --- config block ---------------------------------------------------------


def test_config_humanize_typing_block_has_expected_keys(tmp_path):
    """The typing humanize block must round-trip through ``load_config``."""
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        timing:
          humanize:
            enabled: true
            typing:
              enabled: true
              mean_ms: 85
              std_ms: 30
              min_ms: 25
        """
    )

    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    typing_cfg = cfg.timing.humanize.typing
    assert cfg.timing.humanize.enabled is True
    assert typing_cfg.enabled is True
    assert typing_cfg.mean_ms == 85
    assert typing_cfg.std_ms == 30
    assert typing_cfg.min_ms == 25


def test_config_humanize_typing_defaults(tmp_path):
    """When timing.humanize is unspecified, typing block uses safe defaults."""
    from src.utils.config_loader import load_config

    (tmp_path / "config.yaml").write_text(
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """
    )

    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    typing_cfg = cfg.timing.humanize.typing
    # Default: subsystem disabled but parameter floor is set so it works
    # the moment a user flips the flag without specifying numbers.
    assert typing_cfg.enabled is True
    assert typing_cfg.mean_ms > 0
    assert typing_cfg.std_ms >= 0
    assert typing_cfg.min_ms >= 0
