"""Tests for F1.3 selector registry + ``config/selectors/ticketmaster.yaml``.

Real-Python tests only - no mocks, no fakes. The Playwright Locator check at
the bottom launches a real headless Chromium for a single ``about:blank``
visit and verifies that the registry's ``locator(page, name)`` helper
produces a real :class:`playwright.async_api.Locator` for every logical
name in the YAML.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import src.registry.selectors as selectors_module
from src.registry import selectors as selectors_singleton
from src.registry.base import NotRegistered
from src.registry.selectors import (
    locator,
    locator_multi,
    selector_for,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = REPO_ROOT / "config" / "selectors" / "ticketmaster.yaml"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# Logical names explicitly listed in the F1.3 feature description.
REQUIRED_NAMES = (
    "quick_pick_row",
    "quantity_select",
    "add_to_cart_button",
    "place_order_button",
    "delivery_radio",
    "order_summary",
    "event_onsale_time",
    "best_available_button",
    "sold_out_marker",
    "not_on_sale_marker",
    "queue_position",
    "terms_checkbox",
)


# ---------------------------------------------------------------------------
# YAML shape assertions (covers [refactor.selectors-yaml])
# ---------------------------------------------------------------------------


def test_yaml_file_exists() -> None:
    assert YAML_PATH.is_file(), f"missing {YAML_PATH}"


def test_yaml_top_level_is_dict() -> None:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_yaml_has_at_least_ten_entries() -> None:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert len(data) >= 10, f"expected ≥10 logical-name entries, got {len(data)}"


def test_yaml_every_value_is_non_empty_list_of_non_empty_strings() -> None:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    for name, fallbacks in data.items():
        assert isinstance(fallbacks, list), f"{name!r} is not a list"
        assert len(fallbacks) >= 1, f"{name!r} has no fallbacks"
        for entry in fallbacks:
            assert isinstance(entry, str), f"{name!r} has non-string fallback {entry!r}"
            assert entry.strip(), f"{name!r} has blank fallback"


@pytest.mark.parametrize("name", REQUIRED_NAMES)
def test_required_logical_name_present(name: str) -> None:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    assert name in data, f"missing required logical name {name!r}"


# ---------------------------------------------------------------------------
# Registry surface (loaded from YAML at import time)
# ---------------------------------------------------------------------------


def test_registry_has_every_yaml_entry_after_import() -> None:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    items = selectors_singleton.all()
    for name in data:
        assert name in items, f"{name!r} was not loaded into the selector registry"
        assert items[name] == data[name], (
            f"{name!r} fallback list does not match YAML "
            f"(yaml={data[name]!r}, registry={items[name]!r})"
        )


def test_registry_get_returns_yaml_list_verbatim() -> None:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    sample_name = next(iter(data))
    assert selectors_singleton.get(sample_name) == data[sample_name]


def test_registry_get_unknown_name_raises_with_name_in_message() -> None:
    with pytest.raises(NotRegistered) as exc_info:
        selectors_singleton.get("this_selector_does_not_exist")
    assert "this_selector_does_not_exist" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Public helpers: locator + locator_multi + selector_for
# ---------------------------------------------------------------------------


def test_module_exports_callable_locator() -> None:
    assert callable(locator)
    assert callable(locator_multi)
    assert callable(selector_for)
    # Validation-contract command form: import path and callable property.
    assert callable(selectors_module.locator)


def test_selector_for_joins_with_comma_space() -> None:
    fallbacks = selectors_singleton.get("quick_pick_row")
    joined = selector_for("quick_pick_row")
    assert joined == ", ".join(fallbacks)
    assert ", " in joined or len(fallbacks) == 1


def test_selector_for_unknown_name_raises() -> None:
    with pytest.raises(NotRegistered):
        selector_for("does_not_exist_anywhere_at_all")


def test_validation_contract_callable_subprocess() -> None:
    """Exact command from [refactor.selectors-locator]."""
    result = subprocess.run(
        [
            str(VENV_PYTHON),
            "-c",
            "from src.registry.selectors import locator; print(callable(locator))",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# Real-Playwright check: locator(page, name) returns a real Locator for
# every YAML entry. Covers the second clause of [refactor.selectors-locator].
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locator_returns_playwright_locator_for_every_known_name() -> None:
    from playwright.async_api import Locator, async_playwright

    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            try:
                page = await context.new_page()
                await page.goto("about:blank")
                for name in data:
                    got = locator(page, name)
                    assert isinstance(got, Locator), (
                        f"locator(page, {name!r}) returned {type(got).__name__}, "
                        "expected playwright.async_api.Locator"
                    )
                    got_all = locator_multi(page, name)
                    assert isinstance(got_all, Locator), (
                        f"locator_multi(page, {name!r}) returned {type(got_all).__name__}"
                    )
            finally:
                await context.close()
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Inline-selector gate: no `data-bdd=` may remain in src/**/*.py after F1.3.
# Covered also by [dom.no-inline-selectors] but we ship the guard now.
# ---------------------------------------------------------------------------


def test_no_inline_data_bdd_selectors_in_src() -> None:
    src_dir = REPO_ROOT / "src"
    offenders: list[str] = []
    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "data-bdd=" in line:
                offenders.append(f"{py_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "Inline data-bdd= selector(s) found in src/:\n" + "\n".join(offenders)
