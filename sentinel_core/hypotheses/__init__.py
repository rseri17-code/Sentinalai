"""Hypothesis Intelligence — deterministic hypothesis-path capture.

Placed at ``sentinel_core/hypotheses/`` rather than
``sentinel_core/hypothesis_intelligence/`` because the sentinel_core
package enforces a substring rule against ``"intelligence"``.

Captures the hypothesis lifecycle of an investigation: hypotheses
considered, evidence supporting each, evidence refuting each,
confidence movement, ruled-out causes, final confirmed hypothesis,
MTTI contribution. All deterministic, immutable, JSON-safe. No
runtime mutation, no LLM invocation.
"""
from __future__ import annotations

from sentinel_core.hypotheses.hypothesis_graph import HypothesisGraph
from sentinel_core.hypotheses.hypothesis_tracker import HypothesisTracker
from sentinel_core.hypotheses.report import (
    render_hypothesis_report,
    render_master_report,
    render_ruled_out_report,
    render_scored_report,
    render_summary_report,
    to_json,
)
from sentinel_core.hypotheses.schemas import (
    HYPOTHESIS_SCHEMA_VERSION,
    Hypothesis,
    HypothesisEvidence,
    HypothesisStatus,
    HypothesisTransition,
    make_hypothesis_id,
)
from sentinel_core.hypotheses.scoring import (
    HypothesisScore,
    score_hypothesis,
    score_hypothesis_graph,
)


__all__ = [
    "HYPOTHESIS_SCHEMA_VERSION",
    "Hypothesis",
    "HypothesisEvidence",
    "HypothesisStatus",
    "HypothesisTransition",
    "make_hypothesis_id",
    "HypothesisGraph",
    "HypothesisTracker",
    "HypothesisScore",
    "score_hypothesis",
    "score_hypothesis_graph",
    "render_hypothesis_report",
    "render_summary_report",
    "render_ruled_out_report",
    "render_scored_report",
    "render_master_report",
    "to_json",
]
