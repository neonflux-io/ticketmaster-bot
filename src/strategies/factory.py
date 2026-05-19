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
from .multi_section import MultiSectionStrategy
from .price_range import PriceRangeStrategy
from .random_pick import RandomPickStrategy
from .seat_quality import SeatQualityStrategy
from .section_target import SectionTargetStrategy

if TYPE_CHECKING:
    from ..utils.config_loader import TicketsConfig


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
    }
    for name, cls in defaults.items():
        if name in _strategy_registry.registry:
            continue
        _strategy_registry.register(name, cls)


_register_default_strategies()


def build_strategy(cfg: TicketsConfig) -> SelectionStrategy:
    if cfg.strategy == "cheapest":
        return CheapestStrategy(max_price=cfg.max_price)
    if cfg.strategy == "best_available":
        return BestAvailableStrategy(max_price=cfg.max_price)
    if cfg.strategy == "section_target":
        return SectionTargetStrategy(target=cfg.section_target, max_price=cfg.max_price)
    if cfg.strategy == "price_range":
        return PriceRangeStrategy(
            min_price=cfg.price_range.min_price,
            max_price=cfg.price_range.max_price,
        )
    if cfg.strategy == "multi_section":
        return MultiSectionStrategy(
            sections=list(cfg.multi_section.sections),
            max_price=cfg.max_price,
        )
    if cfg.strategy == "accessible":
        return AccessibleStrategy(
            max_price=cfg.max_price,
            accessible_seats=cfg.accessible_seats,
        )
    if cfg.strategy == "seat_quality":
        return SeatQualityStrategy(max_price=cfg.max_price)
    raise ValueError(f"Unknown strategy: {cfg.strategy!r}")
