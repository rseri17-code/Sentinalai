"""Deterministic JSON report renderers for Strategy Optimizer."""
from __future__ import annotations

import json
from typing import Any, Iterable

from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.strategy_optimizer.mtti_estimator import MttiEstimator
from sentinel_core.strategy_optimizer.optimizer import StrategyOptimizer
from sentinel_core.strategy_optimizer.ranking import StrategyRanker
from sentinel_core.strategy_optimizer.recommendation_engine import (
    StrategyRecommendationEngine,
)
from sentinel_core.strategy_optimizer.schemas import InvestigationStrategy
from sentinel_core.strategy_optimizer.strategy_graph import StrategyGraph


REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Individual reports
# ---------------------------------------------------------------------------

def render_strategy_report(
    strategies: tuple[InvestigationStrategy, ...],
) -> dict[str, Any]:
    ranked = StrategyRanker().rank(strategies)
    return {
        "schema_version":   REPORT_SCHEMA_VERSION,
        "strategy_count":   len(ranked),
        "strategies":       [s.to_dict() for s in ranked],
    }


def render_recommended_strategy(
    strategies: tuple[InvestigationStrategy, ...],
) -> dict[str, Any]:
    ranked = StrategyRanker().rank(strategies)
    top = ranked[0].to_dict() if ranked else None
    return {
        "schema_version":     REPORT_SCHEMA_VERSION,
        "recommended":        top,
        "alternatives":       [s.to_dict() for s in ranked[1:]],
    }


def render_mtti_estimation(
    records: tuple[MemoryRecord, ...],
    current_mtti_ms: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "estimation":     MttiEstimator().estimate(records, current_mtti_ms).to_dict(),
    }


def render_planner_effectiveness(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    g = StrategyGraph().ingest(records)
    top_caps = g.top_capabilities(limit=15)
    per_cap = []
    for cap, cnt in top_caps:
        per_cap.append({
            "capability_id": cap,
            "uses":          cnt,
            "success_rate":  g.capability_success_rate(cap),
        })
    return {
        "schema_version":  REPORT_SCHEMA_VERSION,
        "records_seen":    g.records_seen(),
        "records_success": g.records_success(),
        "per_capability":  sorted(per_cap, key=lambda x: (-x["uses"], x["capability_id"])),
    }


def render_evidence_value_report(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    g = StrategyGraph().ingest(records)
    total = max(1, g.records_seen())
    ranked = []
    for evd, cnt in g.top_evidence(limit=30):
        ranked.append({
            "evidence_key": evd,
            "uses":         cnt,
            "usage_rate":   round(cnt / total, 4),
        })
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "records_seen":   g.records_seen(),
        "per_evidence":   sorted(ranked, key=lambda x: (-x["uses"], x["evidence_key"])),
    }


def render_investigation_efficiency(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    # Efficiency = (confidence × investigation_score) / (1 + runtime_cost)
    # per record; report per-record then aggregate.
    rows = []
    for r in sorted(records, key=lambda x: x.memory_id):
        rc = max(1, int(r.runtime_cost or 0) + 1)
        eff = round((int(r.confidence or 0) / 100.0) *
                     float(r.investigation_score or 0.0) / rc, 6)
        rows.append({
            "memory_id":     r.memory_id,
            "confidence":    int(r.confidence or 0),
            "score":         round(float(r.investigation_score or 0.0), 4),
            "runtime_cost":  int(r.runtime_cost or 0),
            "efficiency":    eff,
        })
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "per_record":     rows,
    }


# ---------------------------------------------------------------------------
# Master report
# ---------------------------------------------------------------------------

def render_master_report(
    records: tuple[MemoryRecord, ...],
    candidate_capabilities: tuple[str, ...] = (),
    registry: dict[str, tuple[str, ...]] | None = None,
    current_mtti_ms: int = 0,
) -> dict[str, Any]:
    g = StrategyGraph().ingest(records)
    strategies = StrategyOptimizer(registry=registry).build_all_strategies(
        candidate_capabilities, g,
    )
    recs = StrategyRecommendationEngine().recommend(records, g)
    return {
        "schema_version":                REPORT_SCHEMA_VERSION,
        "strategy_report":               render_strategy_report(strategies),
        "recommended_strategy":          render_recommended_strategy(strategies),
        "mtti_estimation":               render_mtti_estimation(records, current_mtti_ms),
        "planner_effectiveness":         render_planner_effectiveness(records),
        "evidence_value_report":         render_evidence_value_report(records),
        "investigation_efficiency":      render_investigation_efficiency(records),
        "recommendations":               [r.to_dict() for r in recs],
    }


def to_json(report: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(report, sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_strategy_report",
    "render_recommended_strategy",
    "render_mtti_estimation",
    "render_planner_effectiveness",
    "render_evidence_value_report",
    "render_investigation_efficiency",
    "render_master_report",
    "to_json",
]
