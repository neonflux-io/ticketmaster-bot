"""Tests for src/utils/config_loader.py."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.utils.config_loader import (
    _coerce_optional_float,
    _expand_env,
    _parse_datetime,
    _validate_event_url,
    load_config,
)

# --- _parse_datetime ------------------------------------------------------


def test_parse_datetime_aware():
    dt = _parse_datetime("2026-06-01T10:00:00-05:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_naive_with_fallback_tz():
    dt = _parse_datetime("2026-06-01T10:00:00", fallback_tz="America/New_York")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_naive_without_fallback_raises():
    with pytest.raises(ValueError, match="naive"):
        _parse_datetime("2026-06-01T10:00:00", fallback_tz=None)


def test_parse_datetime_invalid_string():
    with pytest.raises(ValueError):
        _parse_datetime("not-a-date")


def test_parse_datetime_none():
    assert _parse_datetime(None) is None


# --- _coerce_optional_float ----------------------------------------------


def test_coerce_optional_float_none():
    assert _coerce_optional_float(None, "x") is None


def test_coerce_optional_float_string_numeric():
    assert _coerce_optional_float("100", "x") == 100.0


def test_coerce_optional_float_invalid_string():
    with pytest.raises(ValueError, match="must be numeric"):
        _coerce_optional_float("nope", "x")


# --- _expand_env ----------------------------------------------------------


def test_expand_env_replaces(monkeypatch):
    monkeypatch.setenv("ACME_FOO", "bar")
    assert _expand_env("${ACME_FOO}") == "bar"


def test_expand_env_passthrough_non_match():
    assert _expand_env("plain") == "plain"


def test_expand_env_non_string():
    assert _expand_env(42) == 42


# --- _validate_event_url --------------------------------------------------


def test_validate_event_url_trusted():
    _validate_event_url("https://www.ticketmaster.com/event/X", strict=True)


def test_validate_event_url_subdomain_trusted():
    _validate_event_url("https://concerts.livenation.com/x", strict=True)


def test_validate_event_url_untrusted_strict_raises():
    with pytest.raises(ValueError, match="not a known"):
        _validate_event_url("https://example.com/x", strict=True)


def test_validate_event_url_untrusted_warn_only(caplog):
    caplog.set_level("WARNING")
    _validate_event_url("https://example.com/x", strict=False)
    assert "not a known" in caplog.text


def test_validate_event_url_bad_scheme():
    with pytest.raises(ValueError, match="http"):
        _validate_event_url("ftp://example.com", strict=False)


# --- load_config end-to-end ----------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def test_load_config_minimal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert cfg.event.url.endswith("/event/X")
    assert cfg.tickets.quantity == 2
    assert cfg.checkout.auto_purchase is False
    assert cfg.timing.humanize is False
    assert cfg.timing.hold_open_seconds == 600.0
    assert cfg.browser.stealth.enabled is True


def test_load_config_invalid_log_level(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        logging:
          level: "VERBOSE"
        """,
    )
    with pytest.raises(ValueError, match="logging.level"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_invalid_max_price(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          max_price: "nope"
        """,
    )
    with pytest.raises(ValueError, match="max_price"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_strict_host_rejects(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://example.com/event/X"
          strict_host: true
        """,
    )
    with pytest.raises(ValueError, match="not a known"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_invalid_row_range(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "section_target"
          section_target:
            row_range: ["A"]
        """,
    )
    with pytest.raises(ValueError, match="row_range"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")


def test_load_config_accounts_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_PW", "s3cret")
    monkeypatch.setenv("ACME_EMAIL", "user@example.com")
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    _write(
        tmp_path / "accounts.yaml",
        """
        accounts:
          - name: "primary"
            email: "${ACME_EMAIL}"
            password: "${ACME_PW}"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "accounts.yaml")
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].email == "user@example.com"
    assert cfg.accounts[0].password == "s3cret"


def test_load_config_naive_datetime_localized(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
          on_sale_time: "2026-06-01T10:00:00"
        browser:
          timezone: "America/New_York"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.event.on_sale_time is not None
    assert cfg.event.on_sale_time.tzinfo is not None


def test_load_config_env_only_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("TM_EMAIL", "env@example.com")
    monkeypatch.setenv("TM_PASSWORD", "envpw")
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing-accounts.yaml")
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].email == "env@example.com"


# --- price_range strategy config -----------------------------------------


def test_load_config_price_range_strategy_with_min_and_max(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "price_range"
          price_range:
            min_price: 100
            max_price: 300
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.tickets.strategy == "price_range"
    assert cfg.tickets.price_range.min_price == 100.0
    assert cfg.tickets.price_range.max_price == 300.0


def test_load_config_price_range_max_only_is_accepted(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "price_range"
          price_range:
            max_price: 250
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.tickets.price_range.min_price is None
    assert cfg.tickets.price_range.max_price == 250.0


def test_load_config_price_range_without_bounds_rejected(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "price_range"
        """,
    )
    with pytest.raises(ValueError, match="price_range"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_price_range_min_above_max_rejected(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "price_range"
          price_range:
            min_price: 500
            max_price: 100
        """,
    )
    with pytest.raises(ValueError, match="min_price"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_price_range_non_numeric_rejected(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "price_range"
          price_range:
            min_price: "nope"
            max_price: 100
        """,
    )
    with pytest.raises(ValueError, match="min_price"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


# --- multi_section strategy config ---------------------------------------


def test_load_config_multi_section_strategy_loads_ordered_sections(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "multi_section"
          multi_section:
            sections: ["100", "200", "300"]
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.tickets.strategy == "multi_section"
    assert cfg.tickets.multi_section.sections == ["100", "200", "300"]


def test_load_config_multi_section_empty_list_rejected(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "multi_section"
          multi_section:
            sections: []
        """,
    )
    with pytest.raises(ValueError, match="multi_section.sections"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_multi_section_non_string_entries_rejected(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "multi_section"
          multi_section:
            sections: [100, 200]
        """,
    )
    with pytest.raises(ValueError, match="sections"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_unknown_strategy_rejected(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "nope"
        """,
    )
    with pytest.raises(ValueError, match="Invalid tickets.strategy"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


# --- factory build_strategy ----------------------------------------------


def test_build_strategy_price_range_uses_config():
    from src.strategies.factory import build_strategy
    from src.strategies.price_range import PriceRangeStrategy
    from src.utils.config_loader import (
        MultiSectionConfig,
        PriceRangeConfig,
        SectionTargetConfig,
        TicketsConfig,
    )

    cfg = TicketsConfig(
        strategy="price_range",
        section_target=SectionTargetConfig(),
        price_range=PriceRangeConfig(min_price=50.0, max_price=200.0),
        multi_section=MultiSectionConfig(),
    )
    strat = build_strategy(cfg)
    assert isinstance(strat, PriceRangeStrategy)
    assert strat.min_price == 50.0
    assert strat.max_price == 200.0


def test_build_strategy_multi_section_uses_config():
    from src.strategies.factory import build_strategy
    from src.strategies.multi_section import MultiSectionStrategy
    from src.utils.config_loader import (
        MultiSectionConfig,
        PriceRangeConfig,
        SectionTargetConfig,
        TicketsConfig,
    )

    cfg = TicketsConfig(
        strategy="multi_section",
        section_target=SectionTargetConfig(),
        price_range=PriceRangeConfig(),
        multi_section=MultiSectionConfig(sections=["100", "200"]),
        max_price=300.0,
    )
    strat = build_strategy(cfg)
    assert isinstance(strat, MultiSectionStrategy)
    assert strat.sections == ["100", "200"]
    assert strat.max_price == 300.0


def test_strategy_registry_contains_new_strategies():
    """price_range and multi_section must be registered with the strategies
    registry so external code (and entry-point discovery) can find them.
    """
    # Importing the factory triggers default-strategy registration.
    import src.strategies.factory  # noqa: F401
    from src.registry import strategies as strategy_registry
    from src.strategies.multi_section import MultiSectionStrategy
    from src.strategies.price_range import PriceRangeStrategy

    assert strategy_registry.get("price_range") is PriceRangeStrategy
    assert strategy_registry.get("multi_section") is MultiSectionStrategy


# --- wrapper strategies (resale_filter, vfan_aware) ----------------------


def test_load_config_resale_filter_with_inner_strategy(tmp_path):
    """``tickets.strategy='resale_filter'`` loads with a nested
    ``inner_strategy`` and a ``resale_filter`` block."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "resale_filter"
          resale_filter:
            exclude_resale: true
          inner_strategy:
            strategy: "cheapest"
            max_price: 250
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.tickets.strategy == "resale_filter"
    assert cfg.tickets.resale_filter.exclude_resale is True
    assert cfg.tickets.resale_filter.include_resale is False
    assert cfg.tickets.inner_strategy is not None
    assert cfg.tickets.inner_strategy.strategy == "cheapest"
    assert cfg.tickets.inner_strategy.max_price == 250.0


def test_load_config_resale_filter_requires_inner_strategy(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "resale_filter"
          resale_filter:
            exclude_resale: true
        """,
    )
    with pytest.raises(ValueError, match="inner_strategy"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_resale_filter_requires_flag(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "resale_filter"
          inner_strategy:
            strategy: "cheapest"
        """,
    )
    with pytest.raises(ValueError, match="exclude_resale"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_resale_filter_rejects_both_flags(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "resale_filter"
          resale_filter:
            include_resale: true
            exclude_resale: true
          inner_strategy:
            strategy: "cheapest"
        """,
    )
    with pytest.raises(ValueError, match="both"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_vfan_aware_with_inner_strategy(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "vfan_aware"
          vfan_aware:
            code: "ABCD-1234"
          inner_strategy:
            strategy: "price_range"
            price_range:
              min_price: 50
              max_price: 200
        """,
    )
    cfg = load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")
    assert cfg.tickets.strategy == "vfan_aware"
    assert cfg.tickets.vfan_aware.code == "ABCD-1234"
    assert cfg.tickets.inner_strategy is not None
    assert cfg.tickets.inner_strategy.strategy == "price_range"
    assert cfg.tickets.inner_strategy.price_range.min_price == 50.0
    assert cfg.tickets.inner_strategy.price_range.max_price == 200.0


def test_load_config_vfan_aware_requires_code(tmp_path):
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "vfan_aware"
          inner_strategy:
            strategy: "cheapest"
        """,
    )
    with pytest.raises(ValueError, match="vfan_aware.code"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_load_config_inner_strategy_rejects_nested_wrapper(tmp_path):
    """Wrapping a wrapper is not supported — surface the misconfig."""
    _write(
        tmp_path / "config.yaml",
        """
        event:
          url: "https://www.ticketmaster.com/event/X"
        tickets:
          strategy: "resale_filter"
          resale_filter:
            exclude_resale: true
          inner_strategy:
            strategy: "vfan_aware"
            vfan_aware:
              code: "ABCD"
            inner_strategy:
              strategy: "cheapest"
        """,
    )
    with pytest.raises(ValueError, match="wrapper"):
        load_config(tmp_path / "config.yaml", tmp_path / "missing.yaml")


def test_build_strategy_resale_filter_with_cheapest_inner():
    from src.strategies.cheapest import CheapestStrategy
    from src.strategies.factory import build_strategy
    from src.strategies.resale_filter import ResaleFilterStrategy
    from src.utils.config_loader import (
        InnerStrategyConfig,
        MultiSectionConfig,
        PriceRangeConfig,
        ResaleFilterConfig,
        SectionTargetConfig,
        TicketsConfig,
    )

    cfg = TicketsConfig(
        strategy="resale_filter",
        section_target=SectionTargetConfig(),
        price_range=PriceRangeConfig(),
        multi_section=MultiSectionConfig(),
        resale_filter=ResaleFilterConfig(exclude_resale=True),
        inner_strategy=InnerStrategyConfig(
            strategy="cheapest",
            max_price=250.0,
        ),
    )
    strat = build_strategy(cfg)
    assert isinstance(strat, ResaleFilterStrategy)
    assert strat.exclude_resale is True
    assert strat.include_resale is False
    assert isinstance(strat.inner, CheapestStrategy)
    assert strat.inner.max_price == 250.0


def test_build_strategy_vfan_aware_with_price_range_inner():
    from src.strategies.factory import build_strategy
    from src.strategies.price_range import PriceRangeStrategy
    from src.strategies.vfan_aware import VFanAwareStrategy
    from src.utils.config_loader import (
        InnerStrategyConfig,
        MultiSectionConfig,
        PriceRangeConfig,
        SectionTargetConfig,
        TicketsConfig,
        VFanAwareConfig,
    )

    cfg = TicketsConfig(
        strategy="vfan_aware",
        section_target=SectionTargetConfig(),
        price_range=PriceRangeConfig(),
        multi_section=MultiSectionConfig(),
        vfan_aware=VFanAwareConfig(code="ABCD-1234"),
        inner_strategy=InnerStrategyConfig(
            strategy="price_range",
            price_range=PriceRangeConfig(min_price=50.0, max_price=200.0),
        ),
    )
    strat = build_strategy(cfg)
    assert isinstance(strat, VFanAwareStrategy)
    assert strat.code == "ABCD-1234"
    assert isinstance(strat.inner, PriceRangeStrategy)
    assert strat.inner.min_price == 50.0
    assert strat.inner.max_price == 200.0


def test_strategy_registry_contains_resale_filter_and_vfan_aware():
    """The two new wrapper strategies must be in the singleton registry."""
    import src.strategies.factory  # noqa: F401
    from src.registry import strategies as strategy_registry
    from src.strategies.resale_filter import ResaleFilterStrategy
    from src.strategies.vfan_aware import VFanAwareStrategy

    assert strategy_registry.get("resale_filter") is ResaleFilterStrategy
    assert strategy_registry.get("vfan_aware") is VFanAwareStrategy
