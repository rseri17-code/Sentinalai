"""LearningEngine — aggregate deterministic learning scores."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean
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
    FeedbackKind,
)
from sentinel_core.continuous_learning.hypothesis_feedback import (
    HypothesisFeedback,
)
from sentinel_core.continuous_learning.service_learning import ServiceLearning
from sentinel_core.continuous_learning.strategy_feedback import (
    StrategyFeedback,
)
from sentinel_core.intel_memory import MemoryRecord


LEARNING_SCHEMA_VERSION = 1
CONTINUOUS_LEARNING_FEATURE_FLAG = "ENABLE_CONTINUOUS_LEARNING"


@dataclass(frozen=True)
class LearningScores:
    evidence_quality:        float = 0.0
    hypothesis_accuracy:     float = 0.0
    strategy_effectiveness:  float = 0.0
    planner_effectiveness:   float = 0.0
    false_positive_rate:     float = 0.0
    false_negative_rate:     float = 0.0
    service_reliability:     float = 0.0
    root_cause_confidence:   float = 0.0
    replay_agreement:        float = 0.0
    benchmark_agreement:     float = 0.0
    learning_confidence:     float = 0.0
    operational_confidence:  float = 0.0
    schema_version:          int = LEARNING_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in tuple(d.keys()):
            if k == "schema_version":
                continue
            d[k] = round(float(d[k]), 4)
        return d


class LearningEngine:
    """Aggregate every deterministic learning score from the corpus.

    Sub-engines are stateless; the engine holds no mutable state.
    """

    def scores(
        self,
        records: Iterable[MemoryRecord],
        feedback: FeedbackCollector | None = None,
    ) -> LearningScores:
        records = tuple(records or ())
        if not records:
            return LearningScores()

        # Evidence quality — mean quality across evidence keys
        eq_rows = EvidenceQualityScorer().score(records)
        evidence_quality = round(mean(r.quality_score for r in eq_rows), 4) if eq_rows else 0.0

        # Hypothesis accuracy
        h_rows = HypothesisFeedback().score(records)
        hypothesis_accuracy = round(mean(r.accuracy for r in h_rows), 4) if h_rows else 0.0

        # Strategy effectiveness
        s_rows = StrategyFeedback().score(records)
        strategy_effectiveness = round(mean(r.effectiveness for r in s_rows), 4) \
            if s_rows else 0.0
        # Planner effectiveness = same corpus but weighted by uses
        if s_rows:
            weighted = sum(r.effectiveness * r.total_uses for r in s_rows)
            total_uses = sum(r.total_uses for r in s_rows)
            planner_effectiveness = round(weighted / total_uses, 4) if total_uses else 0.0
        else:
            planner_effectiveness = 0.0

        # Success / failure rates
        success = sum(1 for r in records
                        if float(r.investigation_score or 0.0) >= 0.5)
        total = len(records)
        false_negative_rate = round(1.0 - success / total, 4) if total else 0.0
        # False positive rate: sum of |false_leads| / total_evidence_seen
        fp_leads = sum(len(r.false_leads) for r in records)
        total_ev = sum(max(1, len(r.evidence_collected)) for r in records)
        false_positive_rate = round(fp_leads / total_ev, 4) if total_ev else 0.0

        # Service reliability
        svc_rows = ServiceLearning().score(records)
        service_reliability = round(mean(r.success_rate for r in svc_rows), 4) \
            if svc_rows else 0.0

        # Root cause confidence: mean of confidence for records with a root cause
        rc_confs = [int(r.confidence or 0) for r in records if r.detected_root_cause]
        root_cause_confidence = round(mean(rc_confs) / 100.0, 4) if rc_confs else 0.0

        # Replay + benchmark agreement — read from feedback signals when present
        replay_agreement = _agreement_from_signals(feedback, "replay") \
            if feedback else 0.0
        benchmark_agreement = _agreement_from_signals(feedback, "benchmark") \
            if feedback else 0.0
        # Fall back to averaged investigation_score when no feedback
        if not replay_agreement:
            replay_agreement = round(mean(float(r.investigation_score or 0.0)
                                            for r in records), 4)
        if not benchmark_agreement:
            benchmark_agreement = round(mean(float(r.sentinelbench_score or 0.0)
                                                for r in records), 4)

        # Learning confidence — a weighted composite (bounded 0-1)
        components = [
            evidence_quality, hypothesis_accuracy, strategy_effectiveness,
            planner_effectiveness, service_reliability,
            root_cause_confidence, replay_agreement, benchmark_agreement,
        ]
        learning_confidence = round(max(0.0, min(1.0, mean(components))), 4) \
            if components else 0.0
        # Operational confidence = learning_confidence penalised by FP rate
        operational_confidence = round(
            max(0.0, min(1.0, learning_confidence * (1.0 - false_positive_rate))), 4,
        )

        return LearningScores(
            evidence_quality=evidence_quality,
            hypothesis_accuracy=hypothesis_accuracy,
            strategy_effectiveness=strategy_effectiveness,
            planner_effectiveness=planner_effectiveness,
            false_positive_rate=false_positive_rate,
            false_negative_rate=false_negative_rate,
            service_reliability=service_reliability,
            root_cause_confidence=root_cause_confidence,
            replay_agreement=replay_agreement,
            benchmark_agreement=benchmark_agreement,
            learning_confidence=learning_confidence,
            operational_confidence=operational_confidence,
        )


def _agreement_from_signals(feedback: FeedbackCollector, source: str) -> float:
    signals = feedback.by_source(source)
    if not signals:
        return 0.0
    # Convert kinds to a numeric agreement value
    accepts = 0
    total = 0
    for s in signals:
        total += 1
        if s.kind in (FeedbackKind.ROOT_CAUSE_CORRECT.value,
                        FeedbackKind.RESOLUTION_CONFIRMED.value,
                        FeedbackKind.HYPOTHESIS_ACCEPTED.value,
                        FeedbackKind.STRATEGY_APPROVED.value):
            accepts += 1
    return round(accepts / total, 4) if total else 0.0


__all__ = [
    "LEARNING_SCHEMA_VERSION",
    "CONTINUOUS_LEARNING_FEATURE_FLAG",
    "LearningScores",
    "LearningEngine",
]
