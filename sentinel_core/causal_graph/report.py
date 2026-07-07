"""Deterministic JSON renderers for Cross-Incident Causal Graph."""
from __future__ import annotations

import json
from collections import Counter
from statistics import mean
from typing import Any, Iterable

from sentinel_core.causal_graph.chain_detector import ChainDetector
from sentinel_core.causal_graph.graph_builder import CausalGraphBuilder
from sentinel_core.causal_graph.mtti_paths import MTTIPathRanker
from sentinel_core.causal_graph.rca_paths import RCAPathRanker
from sentinel_core.causal_graph.recommendation_engine import (
    CausalRecommendationEngine,
)
from sentinel_core.causal_graph.recurrence import RecurrenceDetector
from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.models._deterministic import canonical_top


REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Individual reports
# ---------------------------------------------------------------------------

def render_causal_graph(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    g = CausalGraphBuilder().build(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "graph":          g.to_dict(),
    }


def render_causal_chains(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    chains = ChainDetector().detect(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "chain_count":    len(chains),
        "chains":         [c.to_dict() for c in chains],
    }


def render_rca_paths(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    paths = RCAPathRanker().build(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "path_count":     len(paths),
        "paths":          [p.to_dict() for p in paths],
    }


def render_mtti_paths(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    paths = MTTIPathRanker().build(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "path_count":     len(paths),
        "paths":          [p.to_dict() for p in paths],
    }


def render_recurrence_report(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    recs = RecurrenceDetector().all_recurrences(records)
    return {
        "schema_version":   REPORT_SCHEMA_VERSION,
        "recurrence_count": len(recs),
        "recurrences":      [r.to_dict() for r in recs],
    }


def render_service_causal_profile(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    per_service: dict[str, list[MemoryRecord]] = {}
    for r in records or ():
        if not r.service:
            continue
        per_service.setdefault(r.service, []).append(r)

    profiles: list[dict[str, Any]] = []
    for svc in sorted(per_service.keys()):
        group = per_service[svc]
        rc_counts = Counter(
            (r.detected_root_cause or "")[:120].strip().lower()
            for r in group if r.detected_root_cause
        )
        mtti_vals = [int(r.mtti_ms or 0) for r in group if int(r.mtti_ms or 0) > 0]
        avg_mtti = int(mean(mtti_vals)) if mtti_vals else 0
        avg_conf = int(mean(int(r.confidence or 0) for r in group)) if group else 0
        profiles.append({
            "service":                svc,
            "incident_count":         len(group),
            "top_root_causes":        [
                # RC-F: canonical_top applies (-count, key) tiebreak so
                # tied root causes always emit in the same order.
                {"root_cause": rc, "count": n}
                for rc, n in canonical_top(rc_counts, 5)
            ],
            "average_mtti_ms":        avg_mtti,
            "average_confidence":     avg_conf,
            "memory_ids":             sorted(r.memory_id for r in group),
        })
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "profile_count":  len(profiles),
        "profiles":       profiles,
    }


def render_causal_recommendations(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    recs = CausalRecommendationEngine().recommend(records)
    return {
        "schema_version":       REPORT_SCHEMA_VERSION,
        "recommendation_count": len(recs),
        "recommendations":      [r.to_dict() for r in recs],
    }


# ---------------------------------------------------------------------------
# Master report
# ---------------------------------------------------------------------------

def render_master_report(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    return {
        "schema_version":            REPORT_SCHEMA_VERSION,
        "causal_graph":              render_causal_graph(records),
        "causal_chains":             render_causal_chains(records),
        "rca_paths":                 render_rca_paths(records),
        "mtti_paths":                render_mtti_paths(records),
        "recurrence_report":         render_recurrence_report(records),
        "service_causal_profile":    render_service_causal_profile(records),
        "causal_recommendations":    render_causal_recommendations(records),
    }


def to_json(report: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(report, sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_causal_graph",
    "render_causal_chains",
    "render_rca_paths",
    "render_mtti_paths",
    "render_recurrence_report",
    "render_service_causal_profile",
    "render_causal_recommendations",
    "render_master_report",
    "to_json",
]
