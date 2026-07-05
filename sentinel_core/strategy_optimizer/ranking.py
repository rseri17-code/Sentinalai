"""StrategyRanker + StrategyClass enum."""
from __future__ import annotations

from enum import Enum

from sentinel_core.strategy_optimizer.schemas import InvestigationStrategy


class StrategyClass(str, Enum):
    BEST                = "best"
    FASTEST             = "fastest"
    HIGHEST_CONFIDENCE  = "highest_confidence"
    LOWEST_COST         = "lowest_cost"
    HIGHEST_SUCCESS     = "highest_success"
    BALANCED            = "balanced"


class StrategyRanker:
    """Rank a list of strategies deterministically."""

    def rank(
        self, strategies: tuple[InvestigationStrategy, ...],
    ) -> tuple[InvestigationStrategy, ...]:
        return tuple(sorted(
            strategies,
            key=lambda s: (-s.overall_value, s.strategy_class, s.strategy_id),
        ))


__all__ = ["StrategyClass", "StrategyRanker"]
