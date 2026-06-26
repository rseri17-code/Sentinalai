"""Tests for supervisor.loop_controller."""
import pytest
from supervisor.loop_controller import (
    EvidenceQualityScorer,
    LoopTelemetry,
    clear_telemetry,
    get_telemetry,
    list_telemetry,
)


class TestEvidenceQualityScorer:
    def setup_method(self):
        self.scorer = EvidenceQualityScorer()

    def test_empty_evidence_scores_zero(self):
        score = self.scorer.score("latency", {})
        assert score == 0.0

    def test_meta_keys_ignored(self):
        evidence = {"_planner_trace": {}, "_loop_telemetry": {}}
        score = self.scorer.score("latency", evidence)
        assert score == 0.0

    def test_error_keys_reduce_score(self):
        full = {"apm_traces": {"data": "ok"}, "splunk_logs": {"data": "ok"}, "metrics": {"data": "ok"}}
        error = {"apm_traces": {"error": "timeout"}, "splunk_logs": {"error": "timeout"}}
        assert self.scorer.score("latency", full) > self.scorer.score("latency", error)

    def test_root_cause_key_boosts_score(self):
        without = {"apm_traces": {"data": "ok"}}
        with_rca = {"apm_traces": {"data": "ok"}, "root_cause": "connection_pool_exhaustion"}
        assert self.scorer.score("latency", with_rca) > self.scorer.score("latency", without)

    def test_convergence_threshold_reachable(self):
        # Provide all expected keys + root_cause — should exceed 0.72
        evidence = {
            "apm_traces": {"latency_p99": 5000},
            "splunk_logs": {"errors": []},
            "metrics": {"cpu": 80},
            "cmdb_blast_radius": {"services": ["svc-a"]},
            "root_cause": "db connection pool exhausted",
        }
        score = self.scorer.score("latency", evidence)
        assert score >= 0.72

    def test_unknown_type_uses_default_keys(self):
        evidence = {"splunk_logs": {"ok": True}, "metrics": {"ok": True}, "cmdb_blast_radius": {"ok": True}}
        score = self.scorer.score("custom_type", evidence)
        assert score > 0.3


class TestLoopTelemetry:
    def test_mtti_zero_when_no_convergence(self):
        t = LoopTelemetry(elapsed_ms=1000.0)
        assert t.mtti_ms == 1000.0

    def test_mtti_fractional_when_converged_early(self):
        t = LoopTelemetry(elapsed_ms=1000.0)
        t.quality_per_iter = [0.5, 0.7, 0.8]
        t.convergence_iter = 1  # converged at iteration 1 of 3
        assert t.mtti_ms < 1000.0

    def test_to_dict_has_required_keys(self):
        t = LoopTelemetry(investigation_id="inv-1", incident_type="latency")
        t.quality_per_iter = [0.4, 0.75]
        t.iterations_run = 2
        d = t.to_dict()
        for key in ("investigation_id", "incident_type", "iterations_run", "final_quality",
                    "nudge_count", "stagnation_detected", "mtti_ms", "elapsed_ms"):
            assert key in d, f"Missing key: {key}"


class TestTelemetryStore:
    def setup_method(self):
        clear_telemetry()

    def test_get_missing_returns_none(self):
        assert get_telemetry("nonexistent") is None

    def test_list_telemetry_empty(self):
        assert list_telemetry() == []
