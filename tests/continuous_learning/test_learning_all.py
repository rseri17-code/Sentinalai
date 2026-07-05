"""Continuous Learning Engine — comprehensive tests."""
from __future__ import annotations

import json
import os

import pytest

from sentinel_core.continuous_learning import (
    CONTINUOUS_LEARNING_FEATURE_FLAG,
    CalibrationBin,
    CausalFeedback,
    ConfidenceCalibrator,
    EvidenceQualityScorer,
    FalsePositiveLearning,
    FeedbackCollector,
    FeedbackKind,
    FeedbackSignal,
    FeedbackSource,
    HypothesisFeedback,
    LearningCycle,
    LearningEngine,
    LearningScores,
    LearningSnapshot,
    OutcomeMemory,
    OutcomeRecord,
    ServiceLearning,
    StrategyFeedback,
    is_enabled,
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
from sentinel_core.intel_memory import MemoryRecord


def _rec(mid, **k):
    d = dict(memory_id=mid)
    d.update(k)
    return MemoryRecord(**d)


def _corpus() -> tuple[MemoryRecord, ...]:
    return (
        _rec("m1",
              service="checkout", incident_type="saturation",
              detected_root_cause="database pool exhausted",
              resolution="scale pool",
              evidence_collected=("oom_events", "logs"),
              planner_decisions=("cap:collect_pod_lifecycle",
                                   "cap:collect_logs"),
              mtti_ms=45000, confidence=85, investigation_score=0.9,
              sentinelbench_score=0.85, false_leads=("certificate",),
              decision_trace={"hypotheses": [
                  {"name": "db pool exhausted", "status": "confirmed"},
                  {"name": "dns failure", "status": "ruled_out"},
              ]}),
        _rec("m2",
              service="checkout", incident_type="saturation",
              detected_root_cause="database pool exhausted",
              resolution="scale pool",
              evidence_collected=("oom_events", "logs"),
              planner_decisions=("cap:collect_pod_lifecycle",
                                   "cap:collect_logs"),
              mtti_ms=52000, confidence=80, investigation_score=0.85,
              sentinelbench_score=0.80),
        _rec("m3",
              service="payments", incident_type="network",
              detected_root_cause="dns nxdomain",
              resolution="reload coredns",
              evidence_collected=("dns_records",),
              planner_decisions=("cap:collect_dns_state",),
              mtti_ms=90000, confidence=40, investigation_score=0.3,
              sentinelbench_score=0.45,
              decision_trace={"hypotheses": [
                  {"name": "dns failure", "status": "confirmed"},
              ]}),
    )


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

class TestFeedback:
    def test_collector_empty(self):
        c = FeedbackCollector()
        assert len(c) == 0

    def test_add_returns_new_instance(self):
        c = FeedbackCollector()
        c2 = c.add(FeedbackSignal(memory_id="m1",
                                    source=FeedbackSource.OPERATOR.value,
                                    kind=FeedbackKind.ROOT_CAUSE_CORRECT.value))
        assert len(c) == 0
        assert len(c2) == 1

    def test_by_source(self):
        c = FeedbackCollector().add(FeedbackSignal(
            memory_id="m1", source="operator",
            kind=FeedbackKind.RESOLUTION_CONFIRMED.value,
        )).add(FeedbackSignal(
            memory_id="m2", source="replay",
            kind=FeedbackKind.ROOT_CAUSE_CORRECT.value,
        ))
        assert len(c.by_source("operator")) == 1

    def test_frozen_signal(self):
        s = FeedbackSignal(memory_id="m", source="operator", kind="k")
        with pytest.raises(Exception):
            s.memory_id = "x"


# ---------------------------------------------------------------------------
# Outcome memory
# ---------------------------------------------------------------------------

class TestOutcomeMemory:
    def test_append_only(self):
        m = OutcomeMemory()
        m2 = m.add(OutcomeRecord(memory_id="m1"))
        assert len(m) == 0
        assert len(m2) == 1

    def test_by_memory_id(self):
        m = OutcomeMemory().add_many([
            OutcomeRecord(memory_id="a"),
            OutcomeRecord(memory_id="b"),
        ])
        assert m.by_memory_id("a")[0].memory_id == "a"


# ---------------------------------------------------------------------------
# Confidence calibration
# ---------------------------------------------------------------------------

class TestConfidenceCalibrator:
    def test_bins_present(self):
        b = ConfidenceCalibrator().calibrate(_corpus())
        assert len(b) == 5

    def test_success_rate_computation(self):
        b = ConfidenceCalibrator().calibrate(_corpus())
        # confidence 80-101 bin has 2 records (m1=85, m2=80), both successful
        target = [x for x in b if x.predicted_lo == 80][0]
        assert target.predicted_count == 2
        assert target.actual_success_rate == 1.0

    def test_deterministic(self):
        b1 = ConfidenceCalibrator().calibrate(_corpus())
        b2 = ConfidenceCalibrator().calibrate(_corpus())
        assert [x.to_dict() for x in b1] == [x.to_dict() for x in b2]


# ---------------------------------------------------------------------------
# Sub-scorers
# ---------------------------------------------------------------------------

class TestSubscorers:
    def test_evidence_quality(self):
        rows = EvidenceQualityScorer().score(_corpus())
        keys = {r.evidence_key for r in rows}
        assert "logs" in keys and "oom_events" in keys

    def test_hypothesis_feedback(self):
        rows = HypothesisFeedback().score(_corpus())
        # "db pool exhausted" was confirmed once → accuracy 1.0
        by = {r.hypothesis: r for r in rows}
        assert by["db pool exhausted"].accuracy == 1.0
        # "dns failure" was ruled_out once + confirmed once → accuracy 0.5
        assert by["dns failure"].accuracy == 0.5

    def test_strategy_feedback(self):
        rows = StrategyFeedback().score(_corpus())
        by = {r.capability_id: r for r in rows}
        # cap:collect_pod_lifecycle: 2 uses, both success → effectiveness 1.0
        assert by["cap:collect_pod_lifecycle"].effectiveness == 1.0

    def test_causal_feedback(self):
        rows = CausalFeedback().score(_corpus())
        # m1+m2 share chain → recurrences 2
        assert any(r.recurrences == 2 for r in rows)

    def test_service_learning(self):
        rows = ServiceLearning().score(_corpus())
        by = {r.service: r for r in rows}
        assert by["checkout"].incident_count == 2
        # success rate: 2/2 = 1.0
        assert by["checkout"].success_rate == 1.0

    def test_false_positive_learning(self):
        rows = FalsePositiveLearning().score(_corpus())
        by = {r.lead: r for r in rows}
        assert by["certificate"].count == 1


# ---------------------------------------------------------------------------
# LearningEngine
# ---------------------------------------------------------------------------

class TestLearningEngine:
    def test_empty(self):
        s = LearningEngine().scores(())
        assert s.evidence_quality == 0.0

    def test_scores_present(self):
        s = LearningEngine().scores(_corpus())
        assert 0.0 <= s.evidence_quality <= 1.0
        assert 0.0 <= s.hypothesis_accuracy <= 1.0
        assert 0.0 <= s.strategy_effectiveness <= 1.0
        assert 0.0 <= s.learning_confidence <= 1.0
        assert 0.0 <= s.operational_confidence <= 1.0

    def test_operational_confidence_penalises_fp(self):
        s = LearningEngine().scores(_corpus())
        assert s.operational_confidence <= s.learning_confidence

    def test_deterministic(self):
        s1 = LearningEngine().scores(_corpus())
        s2 = LearningEngine().scores(_corpus())
        assert s1 == s2


# ---------------------------------------------------------------------------
# LearningCycle
# ---------------------------------------------------------------------------

class TestLearningCycle:
    def test_run_produces_snapshot(self):
        snap = LearningCycle().run(_corpus(), None,
                                     generated_at="2026-07-04T00:00:00Z",
                                     sequence=1)
        assert snap.corpus_size == 3
        assert snap.sequence == 1
        assert snap.snapshot_id

    def test_same_corpus_same_snapshot_id(self):
        s1 = LearningCycle().run(_corpus(), sequence=1)
        s2 = LearningCycle().run(_corpus(), sequence=1)
        assert s1.snapshot_id == s2.snapshot_id

    def test_different_sequence_different_id(self):
        s1 = LearningCycle().run(_corpus(), sequence=1)
        s2 = LearningCycle().run(_corpus(), sequence=2)
        assert s1.snapshot_id != s2.snapshot_id

    def test_append_only_snapshots(self):
        # Simulate append-only ledger
        ledger = []
        for i, seq in enumerate((1, 2, 3)):
            snap = LearningCycle().run(_corpus(), sequence=seq)
            ledger.append(snap)
        # Historical snapshots remain untouched
        assert len(ledger) == 3
        assert all(isinstance(s, LearningSnapshot) for s in ledger)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestReports:
    def test_learning_report(self):
        r = render_learning_report(_corpus())
        assert r["corpus_size"] == 3

    def test_confidence_calibration(self):
        r = render_confidence_calibration(_corpus())
        assert len(r["bins"]) == 5

    def test_strategy_learning(self):
        r = render_strategy_learning(_corpus())
        assert len(r["per_capability"]) >= 1

    def test_hypothesis_learning(self):
        r = render_hypothesis_learning(_corpus())
        names = {x["hypothesis"] for x in r["per_hypothesis"]}
        assert "db pool exhausted" in names

    def test_causal_learning(self):
        r = render_causal_learning(_corpus())
        assert r["chains"]

    def test_service_learning(self):
        r = render_service_learning(_corpus())
        assert any(x["service"] == "checkout"
                     for x in r["per_service"])

    def test_false_positive_report(self):
        r = render_false_positive_report(_corpus())
        assert r["leads"]

    def test_operator_feedback_empty(self):
        r = render_operator_feedback(None)
        assert r["count"] == 0

    def test_operator_feedback_populated(self):
        fb = FeedbackCollector().add(FeedbackSignal(
            memory_id="m1", source="operator",
            kind=FeedbackKind.RESOLUTION_CONFIRMED.value,
            timestamp="2026-07-04",
        ))
        r = render_operator_feedback(fb)
        assert r["count"] == 1

    def test_continuous_learning_summary(self):
        r = render_continuous_learning_summary(_corpus(),
                                                  generated_at="2026-07-04",
                                                  sequence=42)
        assert r["snapshot"]["sequence"] == 42

    def test_master_report_deterministic(self):
        j1 = to_json(render_master_report(_corpus(), sequence=1))
        j2 = to_json(render_master_report(_corpus(), sequence=1))
        assert j1 == j2
        json.loads(j1)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv(CONTINUOUS_LEARNING_FEATURE_FLAG, raising=False)
        assert is_enabled() is False

    def test_env_var_enables(self, monkeypatch):
        monkeypatch.setenv(CONTINUOUS_LEARNING_FEATURE_FLAG, "true")
        assert is_enabled() is True


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_no_forbidden_imports(self):
        import importlib
        for name in ("sentinel_core.continuous_learning.learning_engine",
                      "sentinel_core.continuous_learning.learning_cycle",
                      "sentinel_core.continuous_learning.feedback_collector",
                      "sentinel_core.continuous_learning.confidence_calibrator",
                      "sentinel_core.continuous_learning.evidence_quality",
                      "sentinel_core.continuous_learning.strategy_feedback",
                      "sentinel_core.continuous_learning.hypothesis_feedback",
                      "sentinel_core.continuous_learning.causal_feedback",
                      "sentinel_core.continuous_learning.service_learning",
                      "sentinel_core.continuous_learning.false_positive_learning",
                      "sentinel_core.continuous_learning.outcome_memory",
                      "sentinel_core.continuous_learning.report_renderer"):
            src = open(importlib.import_module(name).__file__).read()
            for banned in ("requests", "httpx", "urllib3", "boto3",
                             "openai", "anthropic", "supervisor.agent",
                             "kubernetes"):
                assert banned not in src, f"{name} imports {banned}"
