"""Deterministic JSON report renderers for Hypothesis Intelligence."""
from __future__ import annotations

import json
from typing import Any

from sentinel_core.hypotheses.hypothesis_graph import HypothesisGraph
from sentinel_core.hypotheses.schemas import Hypothesis, HypothesisStatus
from sentinel_core.hypotheses.scoring import score_hypothesis_graph


REPORT_SCHEMA_VERSION = 1


def render_hypothesis_report(graph: HypothesisGraph) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "graph":          graph.to_dict(),
    }


def render_summary_report(graph: HypothesisGraph) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for h in graph.hypotheses:
        counts[h.status] = counts.get(h.status, 0) + 1
    confirmed = graph.confirmed()
    mtti_sum = sum(int(h.mtti_contribution_ms or 0) for h in graph.hypotheses)
    return {
        "schema_version":   REPORT_SCHEMA_VERSION,
        "investigation_id": graph.investigation_id,
        "hypothesis_count": graph.count(),
        "status_counts":    {k: counts[k] for k in sorted(counts.keys())},
        "confirmed_count":  len(confirmed),
        "ruled_out_count":  len(graph.ruled_out()),
        "confirmed_root_causes": sorted({h.root_cause for h in confirmed if h.root_cause}),
        "total_mtti_contribution_ms": mtti_sum,
    }


def render_ruled_out_report(graph: HypothesisGraph) -> dict[str, Any]:
    entries = []
    for h in sorted(graph.ruled_out(), key=lambda x: x.hypothesis_id):
        entries.append({
            "hypothesis_id":  h.hypothesis_id,
            "name":           h.name,
            "ruled_out_reason": h.ruled_out_reason,
            "refuting_evidence_keys": sorted({e.key for e in h.refuting_evidence}),
        })
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ruled_out":      entries,
    }


def render_scored_report(graph: HypothesisGraph) -> dict[str, Any]:
    scores = score_hypothesis_graph(graph)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "scores":         [s.to_dict() for s in scores],
    }


def render_master_report(graph: HypothesisGraph) -> dict[str, Any]:
    return {
        "schema_version":     REPORT_SCHEMA_VERSION,
        "hypothesis_report":  render_hypothesis_report(graph),
        "summary_report":     render_summary_report(graph),
        "ruled_out_report":   render_ruled_out_report(graph),
        "scored_report":      render_scored_report(graph),
    }


def to_json(report: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(report, sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_hypothesis_report",
    "render_summary_report",
    "render_ruled_out_report",
    "render_scored_report",
    "render_master_report",
    "to_json",
]
