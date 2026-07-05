"""Continuous Learning Engine — deterministic, offline, append-only.

Every completed investigation improves future investigations through
closed-form scoring on the accumulated `MemoryRecord` corpus + optional
feedback signals. Zero LLM. Zero cloud. Zero neural networks.

Feature flag: ``ENABLE_CONTINUOUS_LEARNING`` (default off). The flag is
advisory — the library is always importable. Callers use
:func:`is_enabled` to decide whether to run cycles.
"""
from __future__ import annotations

import os

from sentinel_core.continuous_learning.causal_feedback import CausalFeedback
from sentinel_core.continuous_learning.confidence_calibrator import (
    CalibrationBin,
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
    FeedbackSignal,
    FeedbackSource,
    FeedbackKind,
)
from sentinel_core.continuous_learning.hypothesis_feedback import (
    HypothesisFeedback,
)
from sentinel_core.continuous_learning.learning_cycle import (
    LearningCycle,
    LearningSnapshot,
)
from sentinel_core.continuous_learning.learning_engine import (
    CONTINUOUS_LEARNING_FEATURE_FLAG,
    LearningEngine,
    LearningScores,
    LEARNING_SCHEMA_VERSION,
)
from sentinel_core.continuous_learning.outcome_memory import (
    OutcomeMemory,
    OutcomeRecord,
)
from sentinel_core.continuous_learning.report_renderer import (
    render_causal_learning,
    render_confidence_calibration,
    render_continuous_learning_summary,
    render_false_positive_report,
    render_hypothesis_learning,
    render_learning_report,
    render_master_report,
    render_operator_feedback,
    render_service_learning,
    render_strategy_learning,
    to_json,
)
from sentinel_core.continuous_learning.service_learning import ServiceLearning
from sentinel_core.continuous_learning.strategy_feedback import (
    StrategyFeedback,
)


def is_enabled() -> bool:
    """Return True iff ``ENABLE_CONTINUOUS_LEARNING`` env var is truthy.

    The library is always importable; this helper is for callers that
    want the mission-specified feature-flag semantics.
    """
    return str(os.environ.get(CONTINUOUS_LEARNING_FEATURE_FLAG, "")).lower() \
        in ("1", "true", "yes", "on")


__all__ = [
    "CONTINUOUS_LEARNING_FEATURE_FLAG",
    "LEARNING_SCHEMA_VERSION",
    "is_enabled",
    # engines
    "LearningEngine",
    "LearningCycle",
    "LearningSnapshot",
    "LearningScores",
    # feedback
    "FeedbackCollector",
    "FeedbackSignal",
    "FeedbackSource",
    "FeedbackKind",
    # sub-scorers
    "ConfidenceCalibrator",
    "CalibrationBin",
    "EvidenceQualityScorer",
    "HypothesisFeedback",
    "StrategyFeedback",
    "CausalFeedback",
    "ServiceLearning",
    "FalsePositiveLearning",
    # append-only ledger
    "OutcomeMemory",
    "OutcomeRecord",
    # reports
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
