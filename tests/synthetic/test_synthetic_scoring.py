"""SentinelBench — scoring function tests.

Verifies every scoring dimension is deterministic and behaves
correctly across edge cases.
"""
from __future__ import annotations

import json

import pytest

from tests.synthetic.schemas import Scenario
from tests.synthetic.scoring import (
    DEFAULT_WEIGHTS,
    ScoreCard,
    score_confidence_calibration,
    score_decision_trace_quality,
    score_evidence_completeness,
    score_investigation,
    score_mtti,
    score_red_herring_resistance,
    score_root_cause_match,
    score_runtime_cost,
)


VALID_SCENARIO_DICT = {
    "scenario_id": "unit_score_test",
    "title": "Unit score test",
    "incident_input": {},
    "mocked_evidence_sources": {},
    "expected_root_cause": "database pool exhausted at checkout",
    "required_evidence": ["logs", "metrics_red", "iops"],
    "red_herrings": ["deployment", "certificate"],
    "expected_confidence_range": [60, 80],
    "expected_decision_signals": ["have_prior_resolution_memory",
                                    "have_related_incidents"],
    "expected_mtti_budget_ms": 60000,
    "expected_runtime_cost_budget": 20,
    "tags": [],
    "mock_investigation_output": {
        "root_cause": "database pool exhausted at checkout",
        "confidence": 70,
        "evidence_keys": ["logs", "metrics_red", "iops"],
        "decision_signals": ["have_prior_resolution_memory",
                              "have_related_incidents"],
        "mtti_ms": 45000,
        "runtime_cost": 15,
    },
}


# ---------------------------------------------------------------------------
# Individual dimensions
# ---------------------------------------------------------------------------

class TestRootCauseMatch:
    def test_identical_perfect(self):
        s = score_root_cause_match("pool exhausted", "pool exhausted")
        assert s == 1.0

    def test_overlap_computes_jaccard(self):
        # {"pool", "exhausted"} ∩ {"pool", "recovered"} = 1 / 3
        s = score_root_cause_match("pool exhausted", "pool recovered")
        assert s == pytest.approx(1 / 3, abs=0.001)

    def test_empty_either_returns_zero(self):
        assert score_root_cause_match("", "pool") == 0.0
        assert score_root_cause_match("pool", "") == 0.0

    def test_deterministic(self):
        a = score_root_cause_match("db pool", "db pool exhausted")
        b = score_root_cause_match("db pool", "db pool exhausted")
        assert a == b


class TestEvidenceCompleteness:
    def test_all_present_perfect(self):
        s = score_evidence_completeness(("a", "b"), ("a", "b", "c"))
        assert s == 1.0

    def test_partial(self):
        s = score_evidence_completeness(("a", "b"), ("a",))
        assert s == 0.5

    def test_empty_required_returns_none_not_measured(self):
        """RC-L: empty ground truth ⇒ NOT MEASURED (None), not a
        silent perfect score. Previously returned 1.0 and inflated
        the overall benchmark."""
        assert score_evidence_completeness((), ("anything",)) is None

    def test_empty_reported_zero(self):
        assert score_evidence_completeness(("a",), ()) == 0.0


class TestRedHerringResistance:
    def test_no_red_herrings_present_perfect(self):
        s = score_red_herring_resistance(("deployment", "certificate"),
                                            "database pool exhausted")
        assert s == 1.0

    def test_one_present_partial(self):
        s = score_red_herring_resistance(("deployment", "cert"),
                                            "deployment caused it")
        assert s == 0.5

    def test_all_present_zero(self):
        s = score_red_herring_resistance(("a", "b"), "a and b together")
        assert s == 0.0

    def test_empty_returns_perfect(self):
        assert score_red_herring_resistance((), "anything") == 1.0


class TestConfidenceCalibration:
    def test_in_range_perfect(self):
        assert score_confidence_calibration((60, 80), 70) == 1.0
        assert score_confidence_calibration((60, 80), 60) == 1.0
        assert score_confidence_calibration((60, 80), 80) == 1.0

    def test_slightly_below_falls_off(self):
        # 10 below → 1 - 10/50 = 0.8
        assert score_confidence_calibration((60, 80), 50) == 0.8

    def test_far_above_zero(self):
        assert score_confidence_calibration((60, 80), 200) == 0.0


class TestDecisionTraceQuality:
    def test_all_signals_present_perfect(self):
        s = score_decision_trace_quality(("s1", "s2"), ("s1", "s2", "s3"))
        assert s == 1.0

    def test_partial(self):
        s = score_decision_trace_quality(("s1", "s2"), ("s1",))
        assert s == 0.5

    def test_empty_expected_returns_none_not_measured(self):
        """RC-L: empty ground truth ⇒ NOT MEASURED (None)."""
        assert score_decision_trace_quality((), ("anything",)) is None


class TestRuntimeCost:
    def test_within_budget_perfect(self):
        assert score_runtime_cost(20, 15) == 1.0
        assert score_runtime_cost(20, 20) == 1.0

    def test_over_budget_falls_off(self):
        # 10 over on budget 20 → 1 - 10/20 = 0.5
        assert score_runtime_cost(20, 30) == 0.5

    def test_far_over_budget_zero(self):
        assert score_runtime_cost(20, 200) == 0.0

    def test_zero_budget_edge_cases(self):
        assert score_runtime_cost(0, 0) == 1.0
        assert score_runtime_cost(0, 1) == 0.0


class TestMttiScore:
    def test_within_budget_perfect(self):
        assert score_mtti(60000, 55000) == 1.0

    def test_over_budget_falls_off(self):
        # 30000 over on budget 60000 → 0.5
        assert score_mtti(60000, 90000) == 0.5


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestScoreInvestigation:
    def test_perfect_output_scores_high(self):
        sc = Scenario.from_dict(VALID_SCENARIO_DICT)
        card = score_investigation(sc)
        # Every dimension should be 1.0 given the perfect mock
        assert card.root_cause_match       == 1.0
        assert card.evidence_completeness  == 1.0
        assert card.red_herring_resistance == 1.0
        assert card.confidence_calibration == 1.0
        assert card.decision_trace_quality == 1.0
        assert card.runtime_cost_score     == 1.0
        assert card.mtti_score             == 1.0
        assert card.overall_score          == 1.0
        assert card.scenario_id            == sc.scenario_id
        # Notes flag mock-output path
        assert "scored_against_mock_output" in card.notes

    def test_external_investigation_output_overrides_mock(self):
        sc = Scenario.from_dict(VALID_SCENARIO_DICT)
        io = {
            "root_cause": "completely wrong",
            "confidence": 5,
            "evidence_keys": [],
            "decision_signals": [],
            "mtti_ms": 999999,
            "runtime_cost": 999999,
        }
        card = score_investigation(sc, io)
        assert card.overall_score < 0.3
        assert "scored_against_mock_output" not in card.notes

    def test_degraded_output_reduces_overall(self):
        sc_dict = dict(VALID_SCENARIO_DICT)
        sc_dict["mock_investigation_output"] = {
            "root_cause": "something else entirely",   # low RCA match
            "confidence": 30,                          # out of range (60-80)
            "evidence_keys": [],                       # missing all evidence
            "decision_signals": [],                    # missing all signals
            "mtti_ms": 90000,                          # over budget
            "runtime_cost": 40,                        # over budget
        }
        sc = Scenario.from_dict(sc_dict)
        card = score_investigation(sc)
        assert card.overall_score < 0.4

    def test_deterministic(self):
        sc = Scenario.from_dict(VALID_SCENARIO_DICT)
        c1 = score_investigation(sc)
        c2 = score_investigation(sc)
        assert json.dumps(c1.to_dict(), sort_keys=True) \
            == json.dumps(c2.to_dict(), sort_keys=True)

    def test_custom_weights(self):
        sc = Scenario.from_dict(VALID_SCENARIO_DICT)
        # Weight-only-root-cause-match
        w = {k: 0.0 for k in DEFAULT_WEIGHTS}
        w["root_cause_match"] = 1.0
        c = score_investigation(sc, weights=w)
        assert c.overall_score == 1.0
        # If the ONLY weighted dimension misses, overall goes to 0
        c2 = score_investigation(sc, {"root_cause": "no match at all"}, weights=w)
        assert c2.overall_score == 0.0


class TestWeightsSum:
    def test_default_weights_sum_to_one(self):
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0, abs=0.001)


# ---------------------------------------------------------------------------
# ScoreCard
# ---------------------------------------------------------------------------

class TestScoreCard:
    def test_frozen(self):
        c = ScoreCard(scenario_id="x")
        with pytest.raises(Exception):
            c.scenario_id = "y"

    def test_to_dict_json_safe(self):
        c = ScoreCard(scenario_id="x", overall_score=0.5)
        json.dumps(c.to_dict())   # must not raise
