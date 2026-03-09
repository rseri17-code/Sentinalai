"""Tests for ground truth evaluation framework (supervisor/ground_truth_eval.py).

Covers the GroundTruthEvaluator class: single evaluation, batch evaluation,
root cause matching, ECE computation, and file loading.
"""

from __future__ import annotations

import json

import pytest

from supervisor.ground_truth_eval import (
    GroundTruthEvaluator,
    EvalResult,
    BatchEvalSummary,
)


# =========================================================================
# Fixtures
# =========================================================================

SAMPLE_CORPUS = [
    {
        "incident_id": "INC001",
        "root_cause": "Database connection pool exhaustion",
        "root_cause_keywords": ["connection pool", "database", "exhaustion"],
        "incident_type": "saturation",
        "service": "payment-service",
        "severity": 2,
        "required_evidence": ["logs", "metrics", "golden_signals"],
        "expected_confidence_min": 70,
        "expected_confidence_max": 95,
    },
    {
        "incident_id": "INC002",
        "root_cause": "Memory leak in checkout service",
        "root_cause_keywords": ["memory", "leak", "checkout"],
        "incident_type": "oomkill",
        "service": "checkout-service",
        "severity": 1,
        "required_evidence": ["logs", "metrics"],
        "expected_confidence_min": 60,
        "expected_confidence_max": 90,
    },
]


@pytest.fixture
def evaluator():
    return GroundTruthEvaluator(SAMPLE_CORPUS)


@pytest.fixture
def corpus_file(tmp_path):
    path = tmp_path / "gt.json"
    path.write_text(json.dumps(SAMPLE_CORPUS))
    return str(path)


# =========================================================================
# EvalResult / BatchEvalSummary dataclasses
# =========================================================================

class TestDataclasses:
    def test_eval_result_to_dict(self):
        r = EvalResult(incident_id="INC1", root_cause_match="exact", root_cause_score=1.0)
        d = r.to_dict()
        assert d["incident_id"] == "INC1"
        assert d["root_cause_match"] == "exact"

    def test_batch_summary_to_dict(self):
        s = BatchEvalSummary(total=5, exact_matches=3)
        d = s.to_dict()
        assert d["total"] == 5


# =========================================================================
# GroundTruthEvaluator — constructor and loading
# =========================================================================

class TestEvaluatorInit:
    def test_empty_corpus(self):
        ev = GroundTruthEvaluator([])
        assert ev.incident_ids == []

    def test_corpus_loaded(self, evaluator):
        assert len(evaluator.incident_ids) == 2
        assert "INC001" in evaluator.incident_ids

    def test_has_ground_truth(self, evaluator):
        assert evaluator.has_ground_truth("INC001")
        assert not evaluator.has_ground_truth("INC999")

    def test_skips_entries_without_id(self):
        ev = GroundTruthEvaluator([{"root_cause": "x"}, {"incident_id": "INC1"}])
        assert ev.incident_ids == ["INC1"]


class TestEvaluatorFromFile:
    def test_loads_from_file(self, corpus_file):
        ev = GroundTruthEvaluator.from_file(corpus_file)
        assert len(ev.incident_ids) == 2

    def test_missing_file_returns_empty(self, tmp_path):
        ev = GroundTruthEvaluator.from_file(str(tmp_path / "nonexistent.json"))
        assert ev.incident_ids == []

    def test_invalid_json_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json at all")
        ev = GroundTruthEvaluator.from_file(str(path))
        assert ev.incident_ids == []


# =========================================================================
# Root cause matching
# =========================================================================

class TestRootCauseMatching:
    def test_exact_substring_match(self, evaluator):
        result = {"root_cause": "Database connection pool exhaustion in prod", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.root_cause_match == "exact"
        assert er.root_cause_score == 1.0

    def test_reverse_substring_match(self, evaluator):
        result = {"root_cause": "connection pool exhaustion", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.root_cause_match == "exact"

    def test_keyword_exact_threshold(self, evaluator):
        # All 3 keywords present => ratio = 1.0 >= 0.8 => "exact"
        result = {"root_cause": "connection pool database exhaustion issue", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.root_cause_match == "exact"

    def test_keyword_partial_match(self, evaluator):
        # 2 of 3 keywords => ratio ~0.67 >= 0.4 => "partial"
        result = {"root_cause": "database exhaustion found", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.root_cause_match == "partial"

    def test_miss_no_overlap(self, evaluator):
        result = {"root_cause": "network timeout on proxy", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.root_cause_match == "miss"

    def test_empty_predicted_is_miss(self, evaluator):
        result = {"root_cause": "", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.root_cause_match == "miss"
        assert er.root_cause_score == 0.0

    def test_no_ground_truth_returns_none(self, evaluator):
        result = {"root_cause": "anything", "confidence": 50}
        assert evaluator.evaluate("INC999", result) is None


# =========================================================================
# Confidence calibration
# =========================================================================

class TestConfidenceCalibration:
    def test_within_range_is_calibrated(self, evaluator):
        result = {"root_cause": "connection pool exhaustion", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.confidence_calibrated is True

    def test_below_range_not_calibrated(self, evaluator):
        result = {"root_cause": "connection pool exhaustion", "confidence": 50}
        er = evaluator.evaluate("INC001", result)
        assert er.confidence_calibrated is False

    def test_above_range_not_calibrated(self, evaluator):
        result = {"root_cause": "connection pool exhaustion", "confidence": 99}
        er = evaluator.evaluate("INC001", result)
        assert er.confidence_calibrated is False

    def test_confidence_error_computed(self, evaluator):
        # midpoint = (70+95)/2 = 82.5; error = |80-82.5|/100 = 0.025
        result = {"root_cause": "connection pool exhaustion", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.confidence_error == pytest.approx(0.025, abs=0.001)


# =========================================================================
# Evidence coverage
# =========================================================================

class TestEvidenceCoverage:
    def test_full_coverage(self, evaluator):
        result = {
            "root_cause": "connection pool exhaustion",
            "confidence": 80,
            "evidence_timeline": [
                {"source": "search_logs"},
                {"source": "query_metrics"},
                {"source": "get_golden_signals"},
            ],
        }
        er = evaluator.evaluate("INC001", result)
        assert er.evidence_coverage == 1.0
        assert er.missing_evidence == []

    def test_partial_coverage(self, evaluator):
        result = {
            "root_cause": "connection pool exhaustion",
            "confidence": 80,
            "evidence_timeline": [{"source": "search_logs"}],
        }
        er = evaluator.evaluate("INC001", result)
        assert er.evidence_coverage == pytest.approx(1 / 3, abs=0.01)
        assert "metrics" in er.missing_evidence
        assert "golden_signals" in er.missing_evidence

    def test_no_evidence(self, evaluator):
        result = {"root_cause": "connection pool exhaustion", "confidence": 80}
        er = evaluator.evaluate("INC001", result)
        assert er.evidence_coverage == 0.0

    def test_no_required_evidence_gives_full_coverage(self):
        corpus = [{"incident_id": "INC99", "root_cause": "test", "required_evidence": []}]
        ev = GroundTruthEvaluator(corpus)
        result = {"root_cause": "test", "confidence": 50}
        er = ev.evaluate("INC99", result)
        assert er.evidence_coverage == 1.0


# =========================================================================
# Batch evaluation
# =========================================================================

class TestBatchEvaluation:
    def test_empty_batch(self, evaluator):
        summary = evaluator.evaluate_batch({})
        assert summary.total == 0
        assert summary.accuracy == 0.0

    def test_batch_with_no_matching_ids(self, evaluator):
        results = {"NOPE1": {"root_cause": "x", "confidence": 50}}
        summary = evaluator.evaluate_batch(results)
        assert summary.total == 0

    def test_batch_counts(self, evaluator):
        results = {
            "INC001": {"root_cause": "connection pool database exhaustion", "confidence": 80},
            "INC002": {"root_cause": "totally wrong", "confidence": 50},
        }
        summary = evaluator.evaluate_batch(results)
        assert summary.total == 2
        assert summary.exact_matches + summary.partial_matches + summary.misses == 2
        assert 0.0 <= summary.accuracy <= 1.0

    def test_batch_ece_computed(self, evaluator):
        results = {
            "INC001": {"root_cause": "connection pool exhaustion", "confidence": 80},
            "INC002": {"root_cause": "memory leak checkout", "confidence": 75},
        }
        summary = evaluator.evaluate_batch(results)
        assert isinstance(summary.ece, float)
        assert summary.ece >= 0.0

    def test_batch_results_serializable(self, evaluator):
        results = {"INC001": {"root_cause": "connection pool exhaustion", "confidence": 80}}
        summary = evaluator.evaluate_batch(results)
        d = summary.to_dict()
        assert isinstance(d["results"], list)
        assert len(d["results"]) == 1


# =========================================================================
# ECE computation
# =========================================================================

class TestECEComputation:
    def test_ece_empty_results(self, evaluator):
        ece = evaluator._compute_ece([])
        assert ece == 0.0

    def test_ece_single_result(self, evaluator):
        er = EvalResult(
            incident_id="INC1",
            predicted_confidence=90,
            actual_correct=True,
        )
        ece = evaluator._compute_ece([er])
        assert isinstance(ece, float)
        assert ece >= 0.0

    def test_ece_perfect_calibration(self, evaluator):
        # All predictions at 100% confidence, all correct => ECE should be 0
        results = [
            EvalResult(incident_id=f"I{i}", predicted_confidence=100, actual_correct=True)
            for i in range(10)
        ]
        ece = evaluator._compute_ece(results)
        assert ece == pytest.approx(0.0, abs=0.01)

    def test_ece_worst_calibration(self, evaluator):
        # All predictions at 100% confidence, none correct => ECE ~ 1.0
        results = [
            EvalResult(incident_id=f"I{i}", predicted_confidence=100, actual_correct=False)
            for i in range(10)
        ]
        ece = evaluator._compute_ece(results)
        assert ece > 0.5  # significantly miscalibrated


# =========================================================================
# Jaccard / token overlap matching
# =========================================================================

class TestTokenOverlap:
    def test_jaccard_partial_match(self):
        """Words with >= 50% Jaccard similarity get 'partial'."""
        corpus = [{
            "incident_id": "INC50",
            "root_cause": "slow query primary database",
            "root_cause_keywords": [],
        }]
        ev = GroundTruthEvaluator(corpus)
        # Shares "slow", "query", "database" = 3 shared tokens
        # Union = {"slow","query","primary","database","replica"} = 5
        # Jaccard = 3/5 = 0.6 >= 0.5
        result = {"root_cause": "slow query database replica", "confidence": 60}
        er = ev.evaluate("INC50", result)
        assert er.root_cause_match in ("partial", "exact")

    def test_jaccard_miss_low_overlap(self):
        corpus = [{
            "incident_id": "INC51",
            "root_cause": "certificate expiry on load balancer",
            "root_cause_keywords": [],
        }]
        ev = GroundTruthEvaluator(corpus)
        result = {"root_cause": "disk space full on worker node", "confidence": 60}
        er = ev.evaluate("INC51", result)
        assert er.root_cause_match == "miss"
