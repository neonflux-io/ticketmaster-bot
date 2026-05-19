"""Strategy factory - builds the configured selection strategy.

Every strategy class is registered with the singleton
:mod:`src.registry.strategies` registry at import time so external
plugins can discover them via the ``ticketmaster_bot.strategies``
entry-point group, and :func:`build_strategy` keeps the existing
``TicketsConfig``-driven branching so callers don't have to know about
the registry directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..registry import strategies as _strategy_registry
from .accessible import AccessibleStrategy
from .base import SelectionStrategy
from .best_available import BestAvailableStrategy
from .cheapest import CheapestStrategy
from .composite import CompositeStrategy
from .interactive_seatmap import InteractiveSeatmapStrategy
from .multi_section import MultiSectionStrategy
from .price_range import PriceRangeStrategy
from .random_pick import RandomPickStrategy
from .resale_filter import ResaleFilterStrategy
from .seat_quality import SeatQualityStrategy
from .section_target import SectionTargetStrategy
from .vfan_aware import VFanAwareStrategy

if TYPE_CHECKING:
    from ..utils.config_loader import (
        InnerStrategyConfig,
        MultiSectionConfig,
        PriceRangeConfig,
        SectionTargetConfig,
        TicketsConfig,
    )


# --- registry wiring -------------------------------------------------------


# Register every shipped strategy class under its config key. Calls are
# idempotent across re-imports because the registry raises
# ``DuplicateRegistration`` - we guard with a membership check so re-importing
# (e.g. inside tests that exercise both ``factory`` and the underlying
# modules) doesn't blow up.
def _register_default_strategies() -> None:
    defaults: dict[str, type[SelectionStrategy]] = {
        "cheapest": CheapestStrategy,
        "best_available": BestAvailableStrategy,
        "section_target": SectionTargetStrategy,
        "price_range": PriceRangeStrategy,
        "multi_section": MultiSectionStrategy,
        "accessible": AccessibleStrategy,
        "seat_quality": SeatQualityStrategy,
        "random_pick": RandomPickStrategy,
        "composite": CompositeStrategy,
        "interactive_seatmap": InteractiveSeatmapStrategy,
        "resale_filter": ResaleFilterStrategy,
        "vfan_aware": VFanAwareStrategy,
    }
    for name, cls in defaults.items():
        if name in _strategy_registry.registry:
            continue
        _strategy_registry.register(name, cls)


_register_default_strategies()


def build_strategy(cfg: TicketsConfig) -> SelectionStrategy:
    if cfg.strategy == "resale_filter":
        if cfg.inner_strategy is None:
            raise ValueError(
                "tickets.strategy='resale_filter' requires "
                "tickets.inner_strategy to be set"
            )
        return ResaleFilterStrategy(
            inner=_build_inner_strategy(cfg.inner_strategy),
            include_resale=cfg.resale_filter.include_resale,
            exclude_resale=cfg.resale_filter.exclude_resale,
        )
    if cfg.strategy == "vfan_aware":
        if cfg.inner_strategy is None:
            raise ValueError(
                "tickets.strategy='vfan_aware' requires "
                "tickets.inner_strategy to be set"
            )
        if not cfg.vfan_aware.code:
            raise ValueError(
                "tickets.strategy='vfan_aware' requires tickets.vfan_aware.code"
            )
        return VFanAwareStrategy(
            inner=_build_inner_strategy(cfg.inner_strategy),
            code=cfg.vfan_aware.code,
        )
    return _build_leaf_strategy(
        strategy=cfg.strategy,
        section_target=cfg.section_target,
        price_range=cfg.price_range,
        multi_section=cfg.multi_section,
        max_price=cfg.max_price,
        accessible_seats=cfg.accessible_seats,
    )


def _build_inner_strategy(cfg: InnerStrategyConfig) -> SelectionStrategy:
    """Build the leaf strategy described by a ``tickets.inner_strategy:`` block."""
    return _build_leaf_strategy(
        strategy=cfg.strategy,
        section_target=cfg.section_target,
        price_range=cfg.price_range,
        multi_section=cfg.multi_section,
        max_price=cfg.max_price,
        accessible_seats=cfg.accessible_seats,
    )


def _build_leaf_strategy(
    *,
    strategy: str,
    section_target: SectionTargetConfig,
    price_range: PriceRangeConfig,
    multi_section: MultiSectionConfig,
    max_price: float | None,
    accessible_seats: bool,
) -> SelectionStrategy:
    if strategy == "cheapest":
        return CheapestStrategy(max_price=max_price)
    if strategy == "best_available":
        return BestAvailableStrategy(max_price=max_price)
    if strategy == "section_target":
        return SectionTargetStrategy(target=section_target, max_price=max_price)
    if strategy == "price_range":
        return PriceRangeStrategy(
            min_price=price_range.min_price,
            max_price=price_range.max_price,
        )
    if strategy == "multi_section":
        return MultiSectionStrategy(
            sections=list(multi_section.sections),
            max_price=max_price,
        )
    if strategy == "accessible":
        return AccessibleStrategy(
            max_price=max_price,
            accessible_seats=accessible_seats,
        )
    if strategy == "seat_quality":
        return SeatQualityStrategy(max_price=max_price)
    raise ValueError(f"Unknown strategy: {strategy!r}")
