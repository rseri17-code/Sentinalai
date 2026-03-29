"""Tests for the 4-module self-learning loop.

Covers:
  - supervisor/self_critique.py
  - supervisor/online_evaluator.py
  - supervisor/experience_store.py
  - supervisor/strategy_evolver.py
  - Integration: evolved playbook in tool_selector
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# self_critique
# =============================================================================

class TestSelfCritique:

    def test_returns_perfect_score_when_disabled(self):
        with patch("supervisor.self_critique.SELF_CRITIQUE_ENABLED", False):
            from supervisor.self_critique import critique
            r = critique({"root_cause": "db exhaustion", "confidence": 80}, {}, "timeout")
        assert r.score == 1.0
        assert r.gap_queries == []

    def test_low_confidence_penalised(self):
        from supervisor.self_critique import critique
        result = {
            "root_cause": "INSUFFICIENT EVIDENCE — low confidence",
            "confidence": 10,
            "reasoning": "",
            "evidence_timeline": [],
        }
        cr = critique(result, {}, "timeout", budget_remaining=10)
        assert cr.score < 0.6

    def test_good_result_scores_high(self):
        from supervisor.self_critique import critique
        result = {
            "root_cause": "Database connection pool exhaustion due to memory leak in payment-service",
            "confidence": 78,
            "reasoning": (
                "The logs indicate an exhaustion of connection pool resources. "
                "Metrics confirm memory usage exceeded limits. "
                "Golden signals corroborate latency spike consistent with connection saturation. "
                "Therefore root cause is connection pool exhaustion caused by unclosed connections."
            ),
            "evidence_timeline": [
                {"ts": "t1", "event": "a"},
                {"ts": "t2", "event": "b"},
                {"ts": "t3", "event": "c"},
            ],
        }
        evidence = {
            "search_timeout_logs": [{"msg": "timeout"}],
            "check_golden_signals": {"latency_p99": 3000},
            "query_response_time": {"p99": 3200},
            "get_change_data": [],
        }
        cr = critique(result, evidence, "timeout")
        assert cr.score >= 0.55
        assert cr.dimensions["specificity"] >= 0.4

    def test_gap_queries_generated_below_threshold(self):
        from supervisor.self_critique import critique, CRITIQUE_THRESHOLD
        result = {
            "root_cause": "unknown",
            "confidence": 20,
            "reasoning": "No clear cause found.",
            "evidence_timeline": [],
        }
        # Only logs evidence — missing metrics, signals, events, changes
        evidence = {"search_timeout_logs": [{"msg": "err"}]}
        cr = critique(result, evidence, "timeout", budget_remaining=10)
        # Should identify missing categories and produce gap queries
        assert len(cr.gaps) > 0
        assert len(cr.gap_queries) > 0

    def test_no_gap_queries_when_budget_exhausted(self):
        from supervisor.self_critique import critique
        result = {
            "root_cause": "LOW CONFIDENCE — inconclusive",
            "confidence": 25,
            "reasoning": "",
            "evidence_timeline": [],
        }
        cr = critique(result, {}, "timeout", budget_remaining=0)
        assert cr.gap_queries == []

    def test_evidence_coverage_scoring(self):
        from supervisor.self_critique import _score_evidence_coverage
        # All 5 categories present
        evidence = {
            "search_timeout_logs": ["x"],
            "check_golden_signals": {"a": 1},
            "query_metrics": {"b": 2},
            "get_k8s_events": [{"e": 1}],
            "get_change_data": [{"c": 1}],
        }
        coverage, missing = _score_evidence_coverage(evidence)
        assert coverage >= 0.8
        assert len(missing) <= 1

    def test_evidence_coverage_empty(self):
        from supervisor.self_critique import _score_evidence_coverage
        coverage, missing = _score_evidence_coverage({})
        assert coverage == 0.0
        assert len(missing) == 5


# =============================================================================
# online_evaluator
# =============================================================================

class TestOnlineEvaluator:

    def test_returns_full_score_when_disabled(self):
        with patch("supervisor.online_evaluator.ONLINE_EVAL_ENABLED", False):
            from supervisor.online_evaluator import evaluate
            s = evaluate({}, {})
        assert s.overall == 1.0

    def test_zero_evidence_penalised(self):
        from supervisor.online_evaluator import evaluate
        result = {"root_cause": "unknown", "confidence": 50, "evidence_timeline": []}
        s = evaluate(result, {}, budget_calls_made=1, hypothesis_count=1)
        assert s.overall < 0.5
        assert s.source_count == 0

    def test_good_investigation_scores_well(self):
        from supervisor.online_evaluator import evaluate
        result = {
            "root_cause": "Database connection pool exhaustion causing timeout",
            "confidence": 78,
            "evidence_timeline": [{"ts": "t1"}, {"ts": "t2"}, {"ts": "t3"}],
        }
        evidence = {
            "search_timeout_logs": [{"msg": "timeout"}],
            "check_golden_signals": {"latency": 3000},
            "query_response_time": {"p99": 3200},
        }
        s = evaluate(result, evidence, budget_calls_made=4, hypothesis_count=3)
        assert s.overall >= 0.55
        assert s.source_count >= 2

    def test_insufficient_evidence_gets_low_specificity(self):
        from supervisor.online_evaluator import _score_specificity
        assert _score_specificity("INSUFFICIENT EVIDENCE — low confidence") <= 0.1
        assert _score_specificity("LOW CONFIDENCE — unclear") <= 0.35

    def test_good_root_cause_gets_high_specificity(self):
        from supervisor.online_evaluator import _score_specificity
        score = _score_specificity(
            "Database connection pool exhaustion due to leaked connections in payment-service"
        )
        assert score >= 0.5

    def test_hypothesis_diversity_scaling(self):
        from supervisor.online_evaluator import _score_diversity
        assert _score_diversity(0) < _score_diversity(1)
        assert _score_diversity(1) < _score_diversity(3)
        assert _score_diversity(5) >= 0.85

    def test_annotate_result_injects_keys(self):
        from supervisor.online_evaluator import evaluate, annotate_result
        result: dict = {
            "root_cause": "memory leak",
            "confidence": 70,
            "evidence_timeline": [{"ts": "t1"}, {"ts": "t2"}],
        }
        score = evaluate(result, {"search_timeout_logs": ["x"], "check_golden_signals": {"a": 1}},
                         hypothesis_count=2)
        annotate_result(result, score)
        assert "online_quality_score" in result
        assert "_online_eval" in result
        assert "dimensions" in result["_online_eval"]

    def test_confidence_calibration_dimension(self):
        from supervisor.online_evaluator import _score_calibration
        # 3 sources + 3 timeline = expected ~55; confidence 55 → perfect
        assert _score_calibration(55, 3, [1, 2, 3]) == 1.0
        # Overconfident: 3 sources expect ~55, confidence 95
        overconf = _score_calibration(95, 3, [1, 2, 3])
        assert overconf < 0.7


# =============================================================================
# experience_store
# =============================================================================

class TestExperienceStore:

    @pytest.fixture(autouse=True)
    def temp_store(self, tmp_path, monkeypatch):
        store_path = str(tmp_path / "experience_store.json")
        monkeypatch.setenv("EXPERIENCE_STORE_PATH", store_path)
        # Force the module to re-read the env var
        import supervisor.experience_store as es
        monkeypatch.setattr(es, "EXPERIENCE_STORE_PATH", store_path)
        yield store_path

    def _good_result(self) -> dict:
        return {
            "root_cause": "Database connection pool exhaustion",
            "confidence": 80,
            "_online_eval": {"sources_found": ["logs", "metrics", "signals"]},
        }

    def test_store_and_retrieve(self):
        from supervisor.experience_store import store_experience, retrieve_similar
        r = store_experience("INC001", "timeout", "payment-service", self._good_result(), 0.75)
        assert r is True

        hits = retrieve_similar("timeout", "payment-service")
        assert len(hits) == 1
        assert hits[0]["incident_id"] == "INC001"
        assert "similarity_score" in hits[0]

    def test_below_threshold_not_stored(self):
        from supervisor.experience_store import store_experience, retrieve_similar
        r = store_experience("INC002", "timeout", "svc", self._good_result(), 0.40)
        assert r is False
        hits = retrieve_similar("timeout", "svc")
        assert hits == []

    def test_inconclusive_root_cause_not_stored(self):
        from supervisor.experience_store import store_experience, retrieve_similar
        result = {**self._good_result(), "root_cause": "INSUFFICIENT EVIDENCE — no data"}
        r = store_experience("INC003", "timeout", "svc", result, 0.80)
        assert r is False

    def test_different_type_has_lower_similarity(self):
        from supervisor.experience_store import store_experience, retrieve_similar
        store_experience("INC004", "oomkill", "payment-service", self._good_result(), 0.75)
        store_experience("INC005", "timeout", "payment-service", self._good_result(), 0.75)

        hits = retrieve_similar("timeout", "payment-service")
        assert hits[0]["incident_id"] == "INC005"  # exact type match ranked first

    def test_returns_empty_when_disabled(self):
        import supervisor.experience_store as es
        with patch.object(es, "EXPERIENCE_STORE_ENABLED", False):
            hits = es.retrieve_similar("timeout", "svc")
        assert hits == []

    def test_concurrent_writes_safe(self):
        from supervisor.experience_store import store_experience, retrieve_similar
        errors = []

        def write(i: int):
            try:
                store_experience(
                    f"INC{i:04d}", "timeout", "svc",
                    self._good_result(), 0.70 + i * 0.01,
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        hits = retrieve_similar("timeout", "svc")
        assert len(hits) <= 3  # top_k

    def test_eviction_at_capacity(self, monkeypatch):
        import supervisor.experience_store as es
        monkeypatch.setattr(es, "MAX_EXPERIENCES", 3)
        for i in range(5):
            es.store_experience(
                f"INC{i}", "timeout", "svc",
                self._good_result(), 0.61 + i * 0.01,
            )
        hits = es.retrieve_similar("timeout", "svc", top_k=10)
        assert len(hits) <= 3

    def test_get_stats(self):
        from supervisor.experience_store import store_experience, get_stats
        store_experience("INC_S1", "timeout", "svc-a", self._good_result(), 0.75)
        store_experience("INC_S2", "oomkill", "svc-b", self._good_result(), 0.70)
        stats = get_stats()
        assert stats["count"] == 2
        assert "timeout" in stats["by_type"]
        assert "oomkill" in stats["by_type"]


# =============================================================================
# strategy_evolver
# =============================================================================

class TestStrategyEvolver:

    @pytest.fixture(autouse=True)
    def temp_strategy(self, tmp_path, monkeypatch):
        strategy_path = str(tmp_path / "evolved_strategy.json")
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "EVOLVED_STRATEGY_PATH", strategy_path)
        yield strategy_path

    def _receipts(self, actions: list[str]) -> list[dict]:
        return [
            {"tool": "log_worker", "action": a, "status": "ok", "elapsed_ms": 100}
            for a in actions
        ]

    def test_record_and_retrieve_weights(self, monkeypatch):
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "MIN_CALLS_TO_EVOLVE", 1)
        receipts = self._receipts(["search_timeout_logs", "check_golden_signals"])
        se.record_outcome("timeout", receipts, online_quality_score=0.85)
        weights = se.get_weights("timeout")
        # After 1 call (>= MIN=1), weights should exist
        assert "search_timeout_logs" in weights
        assert "check_golden_signals" in weights

    def test_high_quality_increases_weight(self, monkeypatch):
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "MIN_CALLS_TO_EVOLVE", 1)
        monkeypatch.setattr(se, "EMA_ALPHA", 1.0)  # instant convergence
        receipts = self._receipts(["search_timeout_logs"])
        # High quality → signal = 0.90/0.70 > 1 → weight > 1
        se.record_outcome("timeout", receipts, online_quality_score=0.90)
        weights = se.get_weights("timeout")
        assert weights["search_timeout_logs"] > 1.0

    def test_low_quality_decreases_weight(self, monkeypatch):
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "MIN_CALLS_TO_EVOLVE", 1)
        monkeypatch.setattr(se, "EMA_ALPHA", 1.0)
        receipts = self._receipts(["search_timeout_logs"])
        # Low quality → signal = 0.30/0.70 < 1 → weight < 1
        se.record_outcome("timeout", receipts, online_quality_score=0.30)
        weights = se.get_weights("timeout")
        assert weights["search_timeout_logs"] < 1.0

    def test_disabled_returns_empty(self):
        import supervisor.strategy_evolver as se
        with patch.object(se, "STRATEGY_EVOLVER_ENABLED", False):
            se.record_outcome("timeout", self._receipts(["step_a"]), 0.8)
            assert se.get_weights("timeout") == {}

    def test_min_calls_gate(self, monkeypatch):
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "MIN_CALLS_TO_EVOLVE", 5)
        receipts = self._receipts(["search_timeout_logs"])
        # Only 2 observations — below threshold, should not diverge from 1.0
        se.record_outcome("timeout", receipts, 0.90)
        se.record_outcome("timeout", receipts, 0.90)
        weights = se.get_weights("timeout")
        # Below MIN_CALLS_TO_EVOLVE → not returned in get_weights
        assert "search_timeout_logs" not in weights

    def test_weight_clamped(self, monkeypatch):
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "MIN_CALLS_TO_EVOLVE", 1)
        monkeypatch.setattr(se, "EMA_ALPHA", 1.0)
        # Maximum signal: perfect quality = 1.0/0.70 = 1.43
        receipts = self._receipts(["step_a"])
        se.record_outcome("timeout", receipts, 1.0)
        weights = se.get_weights("timeout")
        assert weights["step_a"] <= 2.0
        # Minimum: near-zero quality → clamped at 0.3
        monkeypatch.setattr(se, "EMA_ALPHA", 1.0)
        se.record_outcome("timeout", receipts, 0.01)
        weights = se.get_weights("timeout")
        assert weights["step_a"] >= 0.3

    def test_get_report(self, monkeypatch):
        import supervisor.strategy_evolver as se
        monkeypatch.setattr(se, "MIN_CALLS_TO_EVOLVE", 1)
        se.record_outcome("timeout", self._receipts(["step_a", "step_b"]), 0.75)
        report = se.get_report()
        assert "incident_types" in report
        assert "timeout" in report["incident_types"]


# =============================================================================
# Integration: get_evolved_playbook
# =============================================================================

class TestGetEvolvedPlaybook:

    def test_returns_base_playbook_when_no_weights(self):
        from supervisor.tool_selector import get_playbook, get_evolved_playbook
        with patch("supervisor.strategy_evolver.get_weights", return_value={}):
            base = get_playbook("timeout")
            evolved = get_evolved_playbook("timeout")
        assert evolved == base

    def test_reorders_steps_with_weights(self):
        from supervisor.tool_selector import get_playbook, get_evolved_playbook
        base = get_playbook("timeout")
        # Give the last step the highest weight
        last_label = base[-1].get("label", base[-1].get("action"))
        weights = {last_label: 2.0}
        with patch("supervisor.strategy_evolver.get_weights", return_value=weights):
            evolved = get_evolved_playbook("timeout")
        assert evolved[0].get("label") == last_label or evolved[0].get("action") == last_label

    def test_falls_back_to_base_on_error(self):
        from supervisor.tool_selector import get_playbook, get_evolved_playbook
        with patch("supervisor.strategy_evolver.get_weights", side_effect=RuntimeError("fail")):
            evolved = get_evolved_playbook("timeout")
        base = get_playbook("timeout")
        assert evolved == base
