"""Strategy Optimizer canonical schemas."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


STRATEGY_SCHEMA_VERSION = 1


class StrategyRecommendationKind(str, Enum):
    RECOMMENDED_ORDER    = "recommended_order"
    SKIP_EVIDENCE        = "skip_evidence"
    PREFER_TOOL          = "prefer_tool"
    PREFER_CAPABILITY    = "prefer_capability"
    AVOID_CAPABILITY     = "avoid_capability"
    DEEPEN_INVESTIGATION = "deepen_investigation"
    SHORTEN_STRATEGY     = "shorten_strategy"


@dataclass(frozen=True)
class StrategyStep:
    """One capability-level step in an investigation strategy.

    Every value is deterministic. ``overall_expected_value`` is a
    closed-form combination of the other fields (see cost_model).
    """
    capability_id:              str
    step_order:                 int
    expected_information_gain:  float = 0.0
    expected_confidence_gain:   int   = 0    # 0-100
    expected_mtti_reduction_ms: int   = 0
    evidence_cost:              int   = 0
    tool_cost:                  int   = 0
    execution_cost:             int   = 0
    historical_success_rate:    float = 0.0
    overall_expected_value:     float = 0.0
    reason:                     str   = ""
    evidence:                   tuple[str, ...] = ()
    schema_version:             int = STRATEGY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("expected_information_gain",
                    "historical_success_rate",
                    "overall_expected_value"):
            d[k] = round(float(d[k]), 4)
        d["evidence"] = list(d["evidence"])
        return d


@dataclass(frozen=True)
class InvestigationStrategy:
    strategy_id:              str
    strategy_class:           str        # "fastest" | "highest_confidence" | ...
    name:                     str
    steps:                    tuple[StrategyStep, ...] = ()
    total_expected_mtti_ms:   int   = 0
    total_expected_cost:      int   = 0
    total_expected_confidence: int  = 0
    overall_value:            float = 0.0
    reason:                   str   = ""
    schema_version:           int   = STRATEGY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":            self.schema_version,
            "strategy_id":               self.strategy_id,
            "strategy_class":            self.strategy_class,
            "name":                      self.name,
            "step_count":                len(self.steps),
            "total_expected_mtti_ms":    self.total_expected_mtti_ms,
            "total_expected_cost":       self.total_expected_cost,
            "total_expected_confidence": self.total_expected_confidence,
            "overall_value":             round(float(self.overall_value), 4),
            "reason":                    self.reason,
            "steps":                     [s.to_dict() for s in self.steps],
        }


@dataclass(frozen=True)
class MttiEstimation:
    current_mtti_ms:            int = 0
    historical_mtti_ms:         int = 0
    expected_mtti_ms:           int = 0
    potential_improvement_ms:   int = 0
    potential_improvement_pct:  float = 0.0
    confidence_interval:        tuple[int, int] = (0, 0)
    sample_size:                int = 0
    schema_version:             int = STRATEGY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["potential_improvement_pct"] = round(float(d["potential_improvement_pct"]), 4)
        d["confidence_interval"] = list(d["confidence_interval"])
        return d


@dataclass(frozen=True)
class StrategyRecommendation:
    kind:              str
    message:           str
    evidence:          tuple[str, ...] = ()
    priority:          int = 100
    related_capabilities: tuple[str, ...] = ()
    schema_version:    int = STRATEGY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":       self.schema_version,
            "kind":                 self.kind,
            "message":              self.message,
            "priority":             self.priority,
            "evidence":             list(self.evidence),
            "related_capabilities": sorted(self.related_capabilities),
        }


# ---------------------------------------------------------------------------
# Deterministic id helpers
# ---------------------------------------------------------------------------

def make_strategy_id(strategy_class: str, steps: tuple[StrategyStep, ...]) -> str:
    raw = strategy_class + ":" + ",".join(s.capability_id for s in steps)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


__all__ = [
    "STRATEGY_SCHEMA_VERSION",
    "StrategyRecommendationKind",
    "StrategyStep",
    "InvestigationStrategy",
    "MttiEstimation",
    "StrategyRecommendation",
    "make_strategy_id",
]
