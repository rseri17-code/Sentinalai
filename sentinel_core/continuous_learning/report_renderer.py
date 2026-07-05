"""Deterministic JSON report renderers for Continuous Learning."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from sentinel_core.continuous_learning.causal_feedback import CausalFeedback
from sentinel_core.continuous_learning.confidence_calibrator import (
    ConfidenceCalibrator,
)
from sentinel_core.continuous_learning.evidence_quality import (
    EvidenceQualityScorer,
)
from sentinel_core.continuous_learning.false_positive_learning import (
    FalsePositiveLearning,
)
from sentinel_core.continuous_learning.feedback_collector import (
    FeedbackCollector,
)
from sentinel_core.continuous_learning.hypothesis_feedback import (
    HypothesisFeedback,
)
from sentinel_core.continuous_learning.learning_cycle import LearningCycle
from sentinel_core.continuous_learning.learning_engine import LearningEngine
from sentinel_core.continuous_learning.service_learning import ServiceLearning
from sentinel_core.continuous_learning.strategy_feedback import (
    StrategyFeedback,
)
from sentinel_core.intel_memory import MemoryRecord


REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Individual renderers
# ---------------------------------------------------------------------------

def render_learning_report(
    records: tuple[MemoryRecord, ...],
    feedback: FeedbackCollector | None = None,
) -> dict[str, Any]:
    scores = LearningEngine().scores(records, feedback)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "corpus_size":    len(records),
        "scores":         scores.to_dict(),
    }


def render_confidence_calibration(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    bins = ConfidenceCalibrator().calibrate(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "bins":           [b.to_dict() for b in bins],
    }


def render_strategy_learning(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    rows = StrategyFeedback().score(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "per_capability": [r.to_dict() for r in rows],
    }


def render_hypothesis_learning(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    rows = HypothesisFeedback().score(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "per_hypothesis": [r.to_dict() for r in rows],
    }


def render_causal_learning(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    rows = CausalFeedback().score(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "chains":         [r.to_dict() for r in rows],
    }


def render_service_learning(
    records: tuple[MemoryRecord, ...],
) -> dict[str, Any]:
    rows = ServiceLearning().score(records)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "per_service":    [r.to_dict() for r in rows],
    }


def render_false_positive_report(
    records: tuple[MemoryRecord, ...],
    feedback: FeedbackCollector | None = None,
) -> dict[str, Any]:
    rows = FalsePositiveLearning().score(records, feedback)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "leads":          [r.to_dict() for r in rows],
    }


def render_operator_feedback(
    feedback: FeedbackCollector | None,
) -> dict[str, Any]:
    if feedback is None:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "count":          0,
            "signals":        [],
            "by_kind":        {},
        }
    signals = feedback.all()
    counts: Counter = Counter(s.kind for s in signals)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "count":          len(signals),
        "signals":        [s.to_dict() for s in
                             sorted(signals, key=lambda s: (s.timestamp,
                                                              s.memory_id, s.kind))],
        "by_kind":        {k: counts[k] for k in sorted(counts.keys())},
    }


def render_continuous_learning_summary(
    records: tuple[MemoryRecord, ...],
    feedback: FeedbackCollector | None = None,
    *,
    generated_at: str = "",
    sequence: int = 0,
) -> dict[str, Any]:
    snap = LearningCycle().run(records, feedback,
                                 generated_at=generated_at,
                                 sequence=sequence)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "snapshot":       snap.to_dict(),
    }


# ---------------------------------------------------------------------------
# Master report
# ---------------------------------------------------------------------------

def render_master_report(
    records: tuple[MemoryRecord, ...],
    feedback: FeedbackCollector | None = None,
    *,
    generated_at: str = "",
    sequence: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version":                REPORT_SCHEMA_VERSION,
        "learning_report":               render_learning_report(records, feedback),
        "confidence_calibration":        render_confidence_calibration(records),
        "strategy_learning":             render_strategy_learning(records),
        "hypothesis_learning":           render_hypothesis_learning(records),
        "causal_learning":               render_causal_learning(records),
        "service_learning":              render_service_learning(records),
        "false_positive_report":         render_false_positive_report(records, feedback),
        "operator_feedback":             render_operator_feedback(feedback),
        "continuous_learning_summary":   render_continuous_learning_summary(
            records, feedback,
            generated_at=generated_at, sequence=sequence,
        ),
    }


def to_json(report: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(report, sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_learning_report",
    "render_confidence_calibration",
    "render_strategy_learning",
    "render_hypothesis_learning",
    "render_causal_learning",
    "render_service_learning",
    "render_false_positive_report",
    "render_operator_feedback",
    "render_continuous_learning_summary",
    "render_master_report",
    "to_json",
]
