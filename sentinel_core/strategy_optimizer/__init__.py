"""Dynamic Investigation Strategy Optimizer.

Deterministic recommendation engine that continuously discovers the
highest-value investigation strategy from Incident Intelligence Memory
+ Hypothesis Intelligence + SentinelReplay + Planner outputs.

Not autonomous execution. Not planner replacement. Recommendations only.
"""
from __future__ import annotations

from sentinel_core.strategy_optimizer.cost_model import (
    CostModel,
    DEFAULT_EVIDENCE_COST,
    DEFAULT_TOOL_COST,
    DEFAULT_SWITCHING_OVERHEAD,
)
from sentinel_core.strategy_optimizer.mtti_estimator import (
    MttiEstimator,
)
from sentinel_core.strategy_optimizer.optimizer import (
    StrategyOptimizer,
)
from sentinel_core.strategy_optimizer.ranking import (
    StrategyClass,
    StrategyRanker,
)
from sentinel_core.strategy_optimizer.recommendation_engine import (
    StrategyRecommendationEngine,
)
from sentinel_core.strategy_optimizer.report import (
    render_evidence_value_report,
    render_investigation_efficiency,
    render_master_report,
    render_mtti_estimation,
    render_planner_effectiveness,
    render_recommended_strategy,
    render_strategy_report,
    to_json,
)
from sentinel_core.strategy_optimizer.schemas import (
    STRATEGY_SCHEMA_VERSION,
    InvestigationStrategy,
    MttiEstimation,
    StrategyRecommendation,
    StrategyRecommendationKind,
    StrategyStep,
)
from sentinel_core.strategy_optimizer.strategy_graph import StrategyGraph


__all__ = [
    "STRATEGY_SCHEMA_VERSION",
    "InvestigationStrategy",
    "MttiEstimation",
    "StrategyRecommendation",
    "StrategyRecommendationKind",
    "StrategyStep",
    "StrategyClass",
    "CostModel",
    "DEFAULT_EVIDENCE_COST",
    "DEFAULT_TOOL_COST",
    "DEFAULT_SWITCHING_OVERHEAD",
    "MttiEstimator",
    "StrategyGraph",
    "StrategyRanker",
    "StrategyOptimizer",
    "StrategyRecommendationEngine",
    "render_strategy_report",
    "render_recommended_strategy",
    "render_mtti_estimation",
    "render_planner_effectiveness",
    "render_evidence_value_report",
    "render_investigation_efficiency",
    "render_master_report",
    "to_json",
]
