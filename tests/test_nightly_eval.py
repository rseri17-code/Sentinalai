"""Tests for scripts.nightly_eval."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from scripts.nightly_eval import (
    run_nightly_eval,
    _compute_quality_metrics,
    _fmt_metrics,
    _build_summary,
    _receipts_from_result,
    _iso_now,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_RESULTS = [
    {
        "incident_id": "INC001",
        "incident_type": "saturation",
        "service": "payment-service",
        "root_cause": "Connection pool exhausted",
        "confidence": 85,
        "citation_coverage": 0.78,
        "online_quality_score": 0.82,
        "_evidence_snapshot": {"logs": True, "metrics": True, "apm_data": False},
        "receipts": [{"worker": "log_worker", "action": "search_logs", "status": "ok"}],
    },
    {
        "incident_id": "INC002",
        "incident_type": "oom_kill",
        "service": "auth-service",
        "root_cause": "Memory leak in session handler",
        "confidence": 70,
        "citation_coverage": 0.65,
        "online_quality_score": 0.75,
        "_evidence_snapshot": {"logs": True, "metrics": True},
        "receipts": [],
    },
]


# ---------------------------------------------------------------------------
# _compute_quality_metrics
# ---------------------------------------------------------------------------

class TestComputeQualityMetrics:

    def test_citation_coverage_averaged(self):
        metrics = _compute_quality_metrics(SAMPLE_RESULTS, {})
        assert "citation_coverage" in metrics
        expected = (0.78 + 0.65) / 2
        assert abs(metrics["citation_coverage"] - expected) < 0.01

    def test_false_positive_rate(self):
        results = [
            {"confidence": 25},  # < 30 → false positive
            {"confidence": 80},  # ok
            {"confidence": 15},  # < 30 → false positive
            {"confidence": 60},  # ok
        ]
        metrics = _compute_quality_metrics(results, {})
        assert "false_positive_rate" in metrics
        assert abs(metrics["false_positive_rate"] - 0.5) < 0.01

    def test_uses_batch_summary_accuracy(self):
        batch_summary = {
            "total": 10,
            "accuracy": 0.80,
            "ece": 0.12,
            "mean_evidence_coverage": 0.88,
        }
        metrics = _compute_quality_metrics(SAMPLE_RESULTS, batch_summary)
        assert metrics["accuracy"] == pytest.approx(0.80)
        assert metrics["calibration_error"] == pytest.approx(0.12)

    def test_evidence_coverage_fallback_from_snapshot(self):
        results_with_snapshot = [
            {"_evidence_snapshot": {"logs": True, "metrics": True, "apm": False}},
        ]
        metrics = _compute_quality_metrics(results_with_snapshot, {"total": 0})
        assert "evidence_coverage" in metrics
        assert 0.0 <= metrics["evidence_coverage"] <= 1.0

    def test_all_values_rounded_to_4dp(self):
        metrics = _compute_quality_metrics(SAMPLE_RESULTS, {})
        for v in metrics.values():
            # Should not have more than 4 decimal places
            assert round(v, 4) == v


# ---------------------------------------------------------------------------
# _receipts_from_result
# ---------------------------------------------------------------------------

class TestReceiptsFromResult:

    def test_returns_receipts_if_present(self):
        result = {"receipts": [{"worker": "log_worker"}]}
        receipts = _receipts_from_result(result)
        assert receipts == [{"worker": "log_worker"}]

    def test_falls_back_to_evidence_snapshot(self):
        result = {
            "receipts": [],
            "_evidence_snapshot": {"logs": True, "metrics": False, "apm": True},
        }
        receipts = _receipts_from_result(result)
        # Only truthy snapshot keys
        workers = {r["worker"] for r in receipts}
        assert "logs" in workers
        assert "apm" in workers
        assert "metrics" not in workers


# ---------------------------------------------------------------------------
# _fmt_metrics
# ---------------------------------------------------------------------------

class TestFmtMetrics:

    def test_returns_string(self):
        s = _fmt_metrics({"accuracy": 0.80, "ece": 0.12})
        assert isinstance(s, str)
        assert "accuracy" in s
        assert "ece" in s

    def test_sorted_alphabetically(self):
        s = _fmt_metrics({"z_metric": 0.5, "a_metric": 0.3})
        assert s.index("a_metric") < s.index("z_metric")


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------

class TestBuildSummary:

    def test_no_regression_in_summary(self):
        report = {
            "regression": {"has_regression": False, "summary": "all ok"},
            "steps": {
                "load_results": {"count": 10},
                "quality_metrics": {"accuracy": 0.80, "citation_coverage": 0.75},
            },
            "elapsed_seconds": 5.2,
            "baseline_updated": True,
        }
        summary = _build_summary(report)
        assert "PASS" in summary
        assert "n=10" in summary

    def test_regression_in_summary(self):
        report = {
            "regression": {"has_regression": True, "summary": "accuracy dropped"},
            "steps": {
                "load_results": {"count": 5},
                "quality_metrics": {"accuracy": 0.60},
            },
            "elapsed_seconds": 3.0,
            "baseline_updated": False,
        }
        summary = _build_summary(report)
        assert "REGRESSION" in summary


# ---------------------------------------------------------------------------
# run_nightly_eval (integration — all external calls mocked)
# ---------------------------------------------------------------------------

class TestRunNightlyEval:

    @patch("scripts.nightly_eval._load_recent_results", return_value=[])
    def test_returns_dict(self, mock_load):
        report = run_nightly_eval(lookback_days=1, dry_run=True)
        assert isinstance(report, dict)
        assert "started_at" in report
        assert "summary" in report

    @patch("scripts.nightly_eval._load_recent_results", return_value=[])
    def test_empty_results_short_circuit(self, mock_load):
        report = run_nightly_eval(lookback_days=1, dry_run=True)
        assert report["steps"]["load_results"]["count"] == 0
        assert "No results" in report.get("summary", "")

    @patch("scripts.nightly_eval._ingest_knowledge_graph", return_value={"ingested": 2, "node_count": 10, "edge_count": 5})
    @patch("scripts.nightly_eval._compact_memory", return_value={"digests_created": 2})
    @patch("scripts.nightly_eval._update_strategy_weights", return_value={"updated_types": 2})
    @patch("scripts.nightly_eval._update_baseline", return_value=False)
    @patch("scripts.nightly_eval._check_regression", return_value={"has_regression": False, "summary": "ok"})
    @patch("scripts.nightly_eval._run_batch_eval", return_value={"total": 2, "accuracy": 0.80, "labelled_count": 2})
    @patch("scripts.nightly_eval._load_recent_results", return_value=SAMPLE_RESULTS)
    def test_full_pipeline_dry_run(
        self, mock_load, mock_batch, mock_regression, mock_baseline,
        mock_strategy, mock_compact, mock_kg,
    ):
        report = run_nightly_eval(lookback_days=7, dry_run=True)
        assert "steps" in report
        assert report["steps"]["load_results"]["count"] == 2
        # Dry run: baseline not updated
        assert report["baseline_updated"] is False
        # Dry run: strategy/memory/kg skipped
        assert report["steps"]["strategy_updates"].get("dry_run") is True

    @patch("scripts.nightly_eval._ingest_knowledge_graph", return_value={"ingested": 2, "node_count": 10, "edge_count": 5})
    @patch("scripts.nightly_eval._compact_memory", return_value={"digests_created": 2})
    @patch("scripts.nightly_eval._update_strategy_weights", return_value={"updated_types": 2})
    @patch("scripts.nightly_eval._update_baseline", return_value=True)
    @patch("scripts.nightly_eval._check_regression", return_value={"has_regression": False, "summary": "ok"})
    @patch("scripts.nightly_eval._run_batch_eval", return_value={"total": 2, "accuracy": 0.80, "labelled_count": 2})
    @patch("scripts.nightly_eval._load_recent_results", return_value=SAMPLE_RESULTS)
    def test_full_pipeline_not_dry_run(
        self, mock_load, mock_batch, mock_regression, mock_baseline,
        mock_strategy, mock_compact, mock_kg,
    ):
        report = run_nightly_eval(lookback_days=7, dry_run=False)
        assert report["baseline_updated"] is True
        assert report["steps"]["strategy_updates"]["updated_types"] == 2
        assert report["steps"]["knowledge_graph"]["ingested"] == 2

    @patch("scripts.nightly_eval._load_recent_results", return_value=SAMPLE_RESULTS)
    @patch("scripts.nightly_eval._run_batch_eval", return_value={"total": 2, "accuracy": 0.80})
    @patch("scripts.nightly_eval._check_regression", return_value={"has_regression": True, "summary": "accuracy dropped"})
    @patch("scripts.nightly_eval._update_baseline", return_value=False)
    @patch("scripts.nightly_eval._update_strategy_weights", return_value={})
    @patch("scripts.nightly_eval._compact_memory", return_value={})
    @patch("scripts.nightly_eval._ingest_knowledge_graph", return_value={"ingested": 0, "node_count": 0, "edge_count": 0})
    def test_regression_flagged_in_report(
        self, mock_kg, mock_compact, mock_strategy, mock_baseline,
        mock_regression, mock_batch, mock_load,
    ):
        report = run_nightly_eval(lookback_days=7, dry_run=False)
        assert report["regression"]["has_regression"] is True
        assert "REGRESSION" in report.get("summary", "")

    @patch("scripts.nightly_eval._load_recent_results", return_value=SAMPLE_RESULTS)
    @patch("scripts.nightly_eval._run_batch_eval", side_effect=Exception("eval exploded"))
    def test_batch_eval_error_does_not_crash(self, mock_batch, mock_load):
        # Even if batch eval fails, nightly eval should not raise
        # (it may propagate exceptions here since _run_batch_eval is not try/excepted
        #  in the calling code — test that the call chain handles it)
        try:
            run_nightly_eval(lookback_days=7, dry_run=True)
        except Exception:
            pytest.fail("nightly_eval raised unexpectedly on eval error")


# ---------------------------------------------------------------------------
# _iso_now
# ---------------------------------------------------------------------------

class TestIsoNow:

    def test_returns_iso_string(self):
        s = _iso_now()
        assert "T" in s
        assert s.endswith("Z")
        assert len(s) == 20
