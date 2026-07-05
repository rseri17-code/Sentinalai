"""SentinelReplay canonical schemas.

Frozen dataclasses used across the replay engine. Every model is
JSON-safe and deterministic — same input → byte-identical output.

- :class:`BenchmarkRun`     — a single stored SentinelBench run.
- :class:`ReplayResult`     — one scenario's replay outcome + delta.
- :class:`Verdict`          — enum of replay verdicts.
- :class:`WeaknessRecord`   — a repeated failure pattern found by the
  learning engine.
- :class:`Recommendation`   — a deterministic learning recommendation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping

from tests.synthetic.scoring import ScoreCard


REPLAY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Verdict(str, Enum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    STABLE = "stable"
    NEW = "new"          # no baseline available


class WeaknessType(str, Enum):
    MISSING_EVIDENCE          = "missing_evidence"
    PLANNER_MISTAKE           = "planner_mistake"
    LOW_CONFIDENCE            = "low_confidence"
    FALSE_POSITIVE            = "false_positive"
    FALSE_NEGATIVE            = "false_negative"
    INCORRECT_ROOT_CAUSE      = "incorrect_root_cause"
    MISSING_TOPOLOGY          = "missing_topology"
    MISSING_DEPENDENCY        = "missing_dependency"
    BLAST_RADIUS_MISTAKE      = "blast_radius_mistake"
    TRANSACTION_PATH_GAP      = "transaction_path_gap"
    RECURRING_INCIDENT_CLASS  = "recurring_incident_class"


class RecommendationKind(str, Enum):
    MISSING_EVIDENCE          = "missing_evidence"
    RECOMMENDED_COLLECTOR     = "recommended_collector"
    RECOMMENDED_PLANNER_CAP   = "recommended_planner_capability"
    RECOMMENDED_SCENARIO      = "recommended_benchmark_scenario"
    RECOMMENDED_KG_ENTITY     = "recommended_knowledge_graph_entity"
    RECOMMENDED_TOPOLOGY      = "recommended_topology_improvement"
    RECOMMENDED_TX_MAPPING    = "recommended_transaction_mapping"
    RECOMMENDED_RCA_PATTERN   = "recommended_rca_pattern"
    RECOMMENDED_INVEST_ORDER  = "recommended_investigation_order"
    RECOMMENDED_MTTI_IMPROVE  = "recommended_mtti_improvement"


# ---------------------------------------------------------------------------
# BenchmarkRun
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BenchmarkRun:
    """One stored SentinelBench run.

    ``generated_at`` is caller-supplied (ISO 8601 string) so tests are
    deterministic. Do not inject a timestamp inside this file.
    """
    run_id:         str
    generated_at:   str = ""
    scorecards:     tuple[ScoreCard, ...] = ()
    metadata:       Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = REPLAY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id":         self.run_id,
            "generated_at":   self.generated_at,
            "metadata":       dict(self.metadata),
            "scorecards":     [c.to_dict() for c in
                                 sorted(self.scorecards, key=lambda s: s.scenario_id)],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BenchmarkRun":
        cards = tuple(_scorecard_from_dict(c) for c in d.get("scorecards", []))
        return cls(
            run_id=str(d["run_id"]),
            generated_at=str(d.get("generated_at", "")),
            scorecards=cards,
            metadata=dict(d.get("metadata", {})),
        )


def _scorecard_from_dict(d: Mapping[str, Any]) -> ScoreCard:
    return ScoreCard(
        scenario_id=str(d.get("scenario_id", "")),
        root_cause_match=float(d.get("root_cause_match", 0.0) or 0.0),
        evidence_completeness=float(d.get("evidence_completeness", 0.0) or 0.0),
        red_herring_resistance=float(d.get("red_herring_resistance", 0.0) or 0.0),
        confidence_calibration=float(d.get("confidence_calibration", 0.0) or 0.0),
        decision_trace_quality=float(d.get("decision_trace_quality", 0.0) or 0.0),
        runtime_cost_score=float(d.get("runtime_cost_score", 0.0) or 0.0),
        mtti_score=float(d.get("mtti_score", 0.0) or 0.0),
        overall_score=float(d.get("overall_score", 0.0) or 0.0),
        weights=dict(d.get("weights", {}) or {}),
        notes=tuple(str(x) for x in (d.get("notes", []) or ())),
    )


# ---------------------------------------------------------------------------
# ReplayResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying one scenario against a baseline."""
    scenario_id:    str
    verdict:        str
    current:        ScoreCard
    baseline:       ScoreCard | None = None
    delta:          Mapping[str, float] = field(default_factory=dict)
    schema_version: int = REPLAY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id":    self.scenario_id,
            "verdict":        self.verdict,
            "current":        self.current.to_dict(),
            "baseline":       self.baseline.to_dict() if self.baseline else None,
            "delta":          {k: round(float(v), 4) for k, v in sorted(self.delta.items())},
        }


# ---------------------------------------------------------------------------
# WeaknessRecord
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeaknessRecord:
    """A repeated weakness found across runs."""
    weakness_type:   str
    scenario_id:     str
    dimension:       str
    count:           int
    average_score:   float
    schema_version:  int = REPLAY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "weakness_type":  self.weakness_type,
            "scenario_id":    self.scenario_id,
            "dimension":      self.dimension,
            "count":          self.count,
            "average_score":  round(self.average_score, 4),
        }


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    """One deterministic learning recommendation.

    ``evidence`` is a tuple of strings explaining WHY this recommendation
    was produced (mission requirement).
    """
    kind:            str
    message:         str
    evidence:        tuple[str, ...] = ()
    priority:        int = 100        # 1-1000, higher = more urgent
    related_scenarios: tuple[str, ...] = ()
    schema_version:  int = REPLAY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind":           self.kind,
            "message":        self.message,
            "priority":       self.priority,
            "evidence":       list(self.evidence),
            "related_scenarios": sorted(self.related_scenarios),
        }


__all__ = [
    "REPLAY_SCHEMA_VERSION",
    "Verdict",
    "WeaknessType",
    "RecommendationKind",
    "BenchmarkRun",
    "ReplayResult",
    "WeaknessRecord",
    "Recommendation",
]
