"""Tests for the typed registry framework in src/registry/.

These tests exercise real Python code paths only - no mocks, no fakes.
Entry-point auto-discovery is exercised by creating a real installable
distribution in a temp dir and adding it to ``sys.path``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from src.registry import hooks, notifiers, selectors, strategies, vendors
from src.registry.base import (
    DuplicateRegistration,
    NotRegistered,
    Registry,
    RegistryError,
)

TYPED_REGISTRY_MODULES = [strategies, vendors, notifiers, hooks, selectors]


# ---------------------------------------------------------------------------
# Module-shape assertions (covers [refactor.registries-exist])
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", TYPED_REGISTRY_MODULES)
def test_typed_registry_module_exposes_register_get_all(module):
    assert hasattr(module, "register")
    assert hasattr(module, "get")
    assert hasattr(module, "all")
    assert callable(module.register)
    assert callable(module.get)
    assert callable(module.all)


@pytest.mark.parametrize("module", TYPED_REGISTRY_MODULES)
def test_typed_registry_singleton_is_registry_instance(module):
    assert isinstance(module.registry, Registry)


def test_each_typed_registry_uses_distinct_singleton():
    singletons = {id(m.registry) for m in TYPED_REGISTRY_MODULES}
    assert len(singletons) == len(TYPED_REGISTRY_MODULES)


def test_typed_registry_kinds_match_module_names():
    assert strategies.registry.kind == "strategies"
    assert vendors.registry.kind == "vendors"
    assert notifiers.registry.kind == "notifiers"
    assert hooks.registry.kind == "hooks"
    assert selectors.registry.kind == "selectors"


def test_typed_registry_entry_point_groups_are_namespaced():
    assert strategies.registry.entry_point_group == "ticketmaster_bot.strategies"
    assert vendors.registry.entry_point_group == "ticketmaster_bot.vendors"
    assert notifiers.registry.entry_point_group == "ticketmaster_bot.notifiers"
    assert hooks.registry.entry_point_group == "ticketmaster_bot.hooks"
    assert selectors.registry.entry_point_group == "ticketmaster_bot.selectors"


# ---------------------------------------------------------------------------
# Behavior of the generic Registry[T] (covers [refactor.registry-duplicate],
# [refactor.registry-missing], and the generic register/get/all contract).
# ---------------------------------------------------------------------------


def test_register_then_get_returns_same_object():
    reg: Registry[object] = Registry("widgets")
    obj = object()
    reg.register("alpha", obj)
    assert reg.get("alpha") is obj


def test_all_returns_a_copy():
    reg: Registry[int] = Registry("widgets")
    reg.register("alpha", 1)
    snapshot = reg.all()
    snapshot["beta"] = 2
    assert "beta" not in reg.all()


def test_all_includes_every_registered_item():
    reg: Registry[str] = Registry("widgets")
    reg.register("a", "A")
    reg.register("b", "B")
    reg.register("c", "C")
    assert reg.all() == {"a": "A", "b": "B", "c": "C"}


def test_duplicate_register_raises_with_name_in_message():
    reg: Registry[object] = Registry("widgets")
    reg.register("alpha", object())
    with pytest.raises(DuplicateRegistration) as exc_info:
        reg.register("alpha", object())
    msg = str(exc_info.value)
    assert "alpha" in msg
    assert "widgets" in msg
    assert exc_info.value.name == "alpha"
    assert exc_info.value.kind == "widgets"


def test_duplicate_register_is_a_registry_error():
    reg: Registry[object] = Registry("widgets")
    reg.register("alpha", object())
    with pytest.raises(RegistryError):
        reg.register("alpha", object())


def test_missing_get_raises_with_requested_name_in_message():
    reg: Registry[object] = Registry("widgets")
    reg.register("alpha", object())
    with pytest.raises(NotRegistered) as exc_info:
        reg.get("does_not_exist")
    msg = str(exc_info.value)
    assert "does_not_exist" in msg
    assert "widgets" in msg
    assert exc_info.value.name == "does_not_exist"
    assert exc_info.value.kind == "widgets"


def test_missing_get_is_a_registry_error():
    reg: Registry[object] = Registry("widgets")
    with pytest.raises(RegistryError):
        reg.get("nope")


def test_contains_reports_membership():
    reg: Registry[int] = Registry("widgets")
    reg.register("a", 1)
    assert "a" in reg
    assert "b" not in reg


def test_iteration_yields_registered_names():
    reg: Registry[int] = Registry("widgets")
    reg.register("a", 1)
    reg.register("b", 2)
    assert sorted(reg) == ["a", "b"]


def test_unregister_removes_entry():
    reg: Registry[int] = Registry("widgets")
    reg.register("a", 1)
    reg.unregister("a")
    assert "a" not in reg


def test_unregister_missing_entry_is_a_noop():
    reg: Registry[int] = Registry("widgets")
    reg.unregister("never-registered")  # must not raise


def test_clear_empties_the_registry():
    reg: Registry[int] = Registry("widgets")
    reg.register("a", 1)
    reg.register("b", 2)
    reg.clear()
    assert reg.all() == {}


# ---------------------------------------------------------------------------
# Entry-point auto-discovery (covers the importlib.metadata path described in
# the feature). Uses a real installable distribution placed on sys.path so
# ``importlib.metadata.entry_points(group=...)`` actually sees it.
# ---------------------------------------------------------------------------


def _install_fake_distribution(
    site_dir: Path,
    dist_name: str,
    entry_points_ini: str,
) -> None:
    """Create a real .dist-info directory on disk that importlib.metadata
    will discover when its parent is on ``sys.path``.
    """
    dist_info = site_dir / f"{dist_name}-0.1.0.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(entry_points_ini, encoding="utf-8")
    (dist_info / "RECORD").write_text("", encoding="utf-8")


def test_entry_point_discovery_loads_real_entry_point(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    _install_fake_distribution(
        site,
        dist_name="tm_bot_fake_plugin_alpha",
        entry_points_ini=("[ticketmaster_bot.test_alpha]\nloaded_obj = sys:version\n"),
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()

    reg: Registry[object] = Registry("test_alpha", entry_point_group="ticketmaster_bot.test_alpha")
    items = reg.all()
    assert "loaded_obj" in items
    assert items["loaded_obj"] is sys.version


def test_entry_point_discovery_runs_only_once(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    _install_fake_distribution(
        site,
        dist_name="tm_bot_fake_plugin_beta",
        entry_points_ini=("[ticketmaster_bot.test_beta]\nfrom_plugin = sys:version\n"),
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()

    reg: Registry[object] = Registry("test_beta", entry_point_group="ticketmaster_bot.test_beta")
    first = reg.all()
    # A subsequent registration of the same name should still raise -
    # entry-point discovery must not run again on the second access.
    with pytest.raises(DuplicateRegistration):
        reg.register("from_plugin", object())
    assert reg.all().keys() == first.keys()


def test_entry_point_discovery_no_matching_group_is_a_clean_noop():
    reg: Registry[object] = Registry(
        "no_such_kind",
        entry_point_group="ticketmaster_bot.nonexistent_group_for_test_only",
    )
    # No registrations from entry points, and no error raised.
    assert reg.all() == {}


def test_local_register_wins_over_entry_point(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    _install_fake_distribution(
        site,
        dist_name="tm_bot_fake_plugin_gamma",
        entry_points_ini=("[ticketmaster_bot.test_gamma]\nwinner = sys:version\n"),
    )
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()

    sentinel = object()
    reg: Registry[object] = Registry("test_gamma", entry_point_group="ticketmaster_bot.test_gamma")
    # Pre-register before any get/all - the local registration must not be
    # overwritten when entry-point discovery runs.
    reg.register("winner", sentinel)
    assert reg.get("winner") is sentinel
    # Discovery has now run; no DuplicateRegistration raised because the
    # name was already present (entry-point discovery yields to local
    # registrations).
    assert reg.get("winner") is sentinel


# ---------------------------------------------------------------------------
# Cross-singleton isolation
# ---------------------------------------------------------------------------


def test_typed_registries_are_isolated_from_each_other():
    # Use a unique key so the test is idempotent across re-runs.
    key = "isolation_probe__do_not_use"
    sentinel = object()
    strategies.register(key, sentinel)
    try:
        assert strategies.get(key) is sentinel
        with pytest.raises(NotRegistered):
            vendors.get(key)
        with pytest.raises(NotRegistered):
            notifiers.get(key)
        with pytest.raises(NotRegistered):
            hooks.get(key)
        with pytest.raises(NotRegistered):
            selectors.get(key)
    finally:
        strategies.registry.unregister(key)
