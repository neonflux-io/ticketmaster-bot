"""Strategy factory - builds the configured selection strategy."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import SelectionStrategy
from .best_available import BestAvailableStrategy
from .cheapest import CheapestStrategy
from .section_target import SectionTargetStrategy

if TYPE_CHECKING:
    from ..utils.config_loader import TicketsConfig


def build_strategy(cfg: TicketsConfig) -> SelectionStrategy:
    if cfg.strategy == "cheapest":
        return CheapestStrategy(max_price=cfg.max_price)
    if cfg.strategy == "best_available":
        return BestAvailableStrategy(max_price=cfg.max_price)
    if cfg.strategy == "section_target":
        return SectionTargetStrategy(target=cfg.section_target, max_price=cfg.max_price)
    raise ValueError(f"Unknown strategy: {cfg.strategy!r}")
