"""Deterministic JSON report renderers for Incident Intelligence Memory."""
from __future__ import annotations

import json
from collections import Counter
from statistics import mean
from typing import Any, Iterable

from sentinel_core.intel_memory.learning import LearningLoop
from sentinel_core.intel_memory.ranking import Ranker
from sentinel_core.intel_memory.recommendation import GuidedInvestigation
from sentinel_core.intel_memory.schemas import (
    MemoryRecord,
    RecurringPattern,
    SimilarityScore,
)


REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Individual reports
# ---------------------------------------------------------------------------

def render_memory_report(records: tuple[MemoryRecord, ...]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "record_count":   len(records),
        "records":        [r.to_dict() for r in sorted(records, key=lambda r: r.memory_id)],
    }


def render_similarity_report(
    query: MemoryRecord, candidates: tuple[MemoryRecord, ...],
    top_n: int = 10, ranker: Ranker | None = None,
) -> dict[str, Any]:
    r = ranker or Ranker()
    scores = r.top_n(query, candidates, n=top_n)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "query_id":       query.memory_id,
        "candidate_count": len(candidates),
        "top_n":          int(top_n),
        "matches":        [s.to_dict() for s in scores],
    }


def render_learning_report(
    records: tuple[MemoryRecord, ...],
    canonical_evidence_set: Iterable[str] = (),
    loop: LearningLoop | None = None,
) -> dict[str, Any]:
    l = loop or LearningLoop()
    patterns = l.all_patterns(records, canonical_evidence_set=canonical_evidence_set)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "record_count":   len(records),
        "pattern_count":  len(patterns),
        "patterns":       [p.to_dict() for p in patterns],
    }


def render_recurring_patterns(
    records: tuple[MemoryRecord, ...],
    loop: LearningLoop | None = None,
) -> dict[str, Any]:
    l = loop or LearningLoop()
    patterns = l.all_patterns(records)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for p in patterns:
        grouped.setdefault(p.kind, []).append(p.to_dict())
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "by_kind":        {k: grouped[k] for k in sorted(grouped.keys())},
    }


def render_incident_clusters(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    """Cluster records by (service, incident_type). Deterministic."""
    buckets: dict[tuple[str, str], list[str]] = {}
    for r in records:
        buckets.setdefault((r.service, r.incident_type), []).append(r.memory_id)
    clusters = []
    for (svc, it), ids in sorted(buckets.items()):
        clusters.append({
            "service":       svc,
            "incident_type": it,
            "count":         len(ids),
            "memory_ids":    sorted(ids),
        })
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "cluster_count":  len(clusters),
        "clusters":       clusters,
    }


def render_guided_investigation(
    query: MemoryRecord, candidates: tuple[MemoryRecord, ...],
    top_n: int = 10,
) -> dict[str, Any]:
    return GuidedInvestigation(top_n=top_n).build(query, candidates)


def render_knowledge_growth(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    """Records sorted by timestamp; running unique-fingerprint count."""
    ordered = sorted(records, key=lambda r: (r.timestamp, r.memory_id))
    seen: set[str] = set()
    growth: list[dict[str, Any]] = []
    for r in ordered:
        seen.add(r.fingerprint)
        growth.append({
            "memory_id":         r.memory_id,
            "timestamp":         r.timestamp,
            "unique_fingerprint_count": len(seen),
        })
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "growth":         growth,
    }


def render_experience_reuse(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    """Fraction of records with a fingerprint that has been seen at
    least twice — a proxy for how often experience is reusable."""
    counts: Counter = Counter(r.fingerprint for r in records if r.fingerprint)
    reused_fp = {fp for fp, n in counts.items() if n >= 2}
    reused = sum(1 for r in records if r.fingerprint in reused_fp)
    return {
        "schema_version":     REPORT_SCHEMA_VERSION,
        "record_count":       len(records),
        "reuse_fingerprints": sorted(reused_fp),
        "reused_records":     reused,
        "reuse_rate":         round(reused / len(records), 4) if records else 0.0,
    }


def render_top_root_causes(
    records: tuple[MemoryRecord, ...], top_n: int = 10,
) -> dict[str, Any]:
    counts: Counter = Counter(
        (r.detected_root_cause[:120].strip()) for r in records
        if r.detected_root_cause
    )
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: max(0, int(top_n))]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "top_root_causes": [{"root_cause": rc, "count": n} for rc, n in ranked],
    }


def render_top_false_leads(
    records: tuple[MemoryRecord, ...], top_n: int = 10,
) -> dict[str, Any]:
    counts: Counter = Counter()
    for r in records:
        for lead in r.false_leads:
            counts[str(lead).lower()] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: max(0, int(top_n))]
    return {
        "schema_version":  REPORT_SCHEMA_VERSION,
        "top_false_leads": [{"lead": rc, "count": n} for rc, n in ranked],
    }


def render_mtti_improvement(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    """MTTI over time (per record) + linear slope descriptor."""
    ordered = sorted(records, key=lambda r: (r.timestamp, r.memory_id))
    series = [
        {"memory_id": r.memory_id, "timestamp": r.timestamp,
          "mtti_ms": int(r.mtti_ms or 0)}
        for r in ordered
    ]
    mtti_values = [s["mtti_ms"] for s in series]
    if len(mtti_values) >= 2:
        first = mtti_values[0]
        last  = mtti_values[-1]
        slope = round((last - first) / (len(mtti_values) - 1), 4)
        if slope < -1.0:
            direction = "improving"
        elif slope > 1.0:
            direction = "regressing"
        else:
            direction = "stable"
    else:
        slope = 0.0
        direction = "stable"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "series":         series,
        "average_mtti_ms": int(mean(mtti_values)) if mtti_values else 0,
        "slope_ms_per_step": slope,
        "direction":      direction,
    }


# ---------------------------------------------------------------------------
# Master report
# ---------------------------------------------------------------------------

def render_master_report(
    records: tuple[MemoryRecord, ...],
    query: MemoryRecord | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Every report in one payload. Deterministic."""
    body: dict[str, Any] = {
        "schema_version":       REPORT_SCHEMA_VERSION,
        "memory_report":        render_memory_report(records),
        "learning_report":      render_learning_report(records),
        "recurring_patterns":   render_recurring_patterns(records),
        "incident_clusters":    render_incident_clusters(records),
        "knowledge_growth":     render_knowledge_growth(records),
        "experience_reuse":     render_experience_reuse(records),
        "top_root_causes":      render_top_root_causes(records, top_n=top_n),
        "top_false_leads":      render_top_false_leads(records, top_n=top_n),
        "mtti_improvement":     render_mtti_improvement(records),
    }
    if query is not None:
        body["similarity_report"] = render_similarity_report(
            query, records, top_n=top_n,
        )
        body["guided_investigation"] = render_guided_investigation(
            query, records, top_n=top_n,
        )
    return body


def to_json(report: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(report, sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_memory_report",
    "render_similarity_report",
    "render_learning_report",
    "render_recurring_patterns",
    "render_incident_clusters",
    "render_guided_investigation",
    "render_knowledge_growth",
    "render_experience_reuse",
    "render_top_root_causes",
    "render_top_false_leads",
    "render_mtti_improvement",
    "render_master_report",
    "to_json",
]
