"""Tests for supervisor.regression_harness."""
from __future__ import annotations

import pytest

from supervisor.regression_harness import (
    RegressionBaseline,
    RegressionReport,
    check_regression,
    update_baseline_if_better,
    load_baseline,
    _save_baseline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_baseline_path(tmp_path, monkeypatch):
    """Use a temp file for the baseline during tests."""
    path = str(tmp_path / "regression_baseline.json")
    monkeypatch.setattr("supervisor.regression_harness.BASELINE_PATH", path)
    return path


GOOD_METRICS = {
    "accuracy": 0.85,
    "calibration_error": 0.12,
    "evidence_coverage": 0.90,
    "citation_coverage": 0.78,
    "false_positive_rate": 0.08,
}

BASELINE_METRICS = {
    "accuracy": 0.80,
    "calibration_error": 0.15,
    "evidence_coverage": 0.85,
    "citation_coverage": 0.72,
    "false_positive_rate": 0.10,
}


# ---------------------------------------------------------------------------
# RegressionBaseline
# ---------------------------------------------------------------------------

class TestRegressionBaseline:

    def test_defaults(self):
        b = RegressionBaseline()
        assert b.accuracy == 0.0
        assert b.calibration_error == 1.0
        assert b.sample_count == 0

    def test_to_dict_keys(self):
        b = RegressionBaseline(accuracy=0.75, sample_count=10)
        d = b.to_dict()
        assert "accuracy" in d
        assert "calibration_error" in d
        assert "sample_count" in d
        assert d["accuracy"] == 0.75


# ---------------------------------------------------------------------------
# RegressionReport
# ---------------------------------------------------------------------------

class TestRegressionReport:

    def test_summary_no_regression(self):
        report = RegressionReport(has_regression=False, improvements=[{"metric": "accuracy"}])
        s = report.summary()
        assert "No regressions" in s
        assert "1 improvements" in s

    def test_summary_with_regression(self):
        report = RegressionReport(
            has_regression=True,
            regressions=[{
                "metric": "accuracy", "baseline": 0.80, "current": 0.72, "delta": -0.08
            }],
        )
        s = report.summary()
        assert "REGRESSIONS" in s
        assert "accuracy" in s


# ---------------------------------------------------------------------------
# check_regression
# ---------------------------------------------------------------------------

class TestCheckRegression:

    def test_no_baseline_passes(self, tmp_baseline_path):
        # No baseline file → no regression (empty baseline)
        report = check_regression(GOOD_METRICS)
        assert report.has_regression is False

    def test_regression_detected_accuracy_drop(self, tmp_baseline_path):
        # Save a baseline that is better than current
        baseline = RegressionBaseline(
            accuracy=0.90,  # current is 0.85 → delta = -0.05 = threshold
            calibration_error=0.12,
            evidence_coverage=0.85,
            citation_coverage=0.72,
            false_positive_rate=0.10,
            sample_count=20,
        )
        _save_baseline(baseline)

        # Metrics that regress on accuracy (drop > threshold of 0.05)
        bad_metrics = {**GOOD_METRICS, "accuracy": 0.82}  # -0.08 drop
        report = check_regression(bad_metrics)
        assert report.has_regression is True
        reg_metrics = {r["metric"] for r in report.regressions}
        assert "accuracy" in reg_metrics

    def test_no_regression_when_metrics_better(self, tmp_baseline_path):
        baseline = RegressionBaseline(**{**BASELINE_METRICS, "sample_count": 20})
        _save_baseline(baseline)

        # Metrics are strictly better
        report = check_regression(GOOD_METRICS)
        assert report.has_regression is False

    def test_calibration_error_regression(self, tmp_baseline_path):
        # ECE rises more than threshold (0.10)
        baseline = RegressionBaseline(
            accuracy=0.80, calibration_error=0.10,
            evidence_coverage=0.85, citation_coverage=0.72,
            false_positive_rate=0.10, sample_count=10,
        )
        _save_baseline(baseline)

        bad_metrics = {**GOOD_METRICS, "calibration_error": 0.25}  # +0.15 rise
        report = check_regression(bad_metrics)
        assert report.has_regression is True
        reg_metrics = {r["metric"] for r in report.regressions}
        assert "calibration_error" in reg_metrics

    def test_unknown_metrics_skipped(self, tmp_baseline_path):
        baseline = RegressionBaseline(**{**BASELINE_METRICS, "sample_count": 10})
        _save_baseline(baseline)
        report = check_regression({"some_unknown_metric": 0.5})
        # Unknown keys don't trigger regression
        assert report.has_regression is False

    def test_partial_metrics_ok(self, tmp_baseline_path):
        baseline = RegressionBaseline(**{**BASELINE_METRICS, "sample_count": 10})
        _save_baseline(baseline)
        # Only pass accuracy
        report = check_regression({"accuracy": 0.80})
        assert isinstance(report.has_regression, bool)


# ---------------------------------------------------------------------------
# update_baseline_if_better
# ---------------------------------------------------------------------------

class TestUpdateBaselineIfBetter:

    def test_updates_when_better(self, tmp_baseline_path):
        # No existing baseline
        updated = update_baseline_if_better(GOOD_METRICS, sample_count=10)
        assert updated is True
        loaded = load_baseline()
        assert loaded.accuracy == pytest.approx(GOOD_METRICS["accuracy"])

    def test_does_not_update_when_worse(self, tmp_baseline_path):
        # Save a good baseline first
        baseline = RegressionBaseline(
            accuracy=0.95, calibration_error=0.05,
            evidence_coverage=0.95, citation_coverage=0.90,
            false_positive_rate=0.03, sample_count=20,
        )
        _save_baseline(baseline)

        # Try to update with worse metrics
        bad_metrics = {
            "accuracy": 0.50, "calibration_error": 0.50,
            "evidence_coverage": 0.50, "citation_coverage": 0.50,
            "false_positive_rate": 0.50,
        }
        updated = update_baseline_if_better(bad_metrics, sample_count=5)
        assert updated is False

    def test_min_sample_count_enforced(self, tmp_baseline_path):
        # sample_count < 5 should never update
        updated = update_baseline_if_better(GOOD_METRICS, sample_count=3)
        assert updated is False

    def test_ratchet_baseline_only_improves(self, tmp_baseline_path):
        # First update
        update_baseline_if_better(GOOD_METRICS, sample_count=10)
        first = load_baseline()

        # Second update with slightly better
        better = {**GOOD_METRICS, "accuracy": 0.90}
        update_baseline_if_better(better, sample_count=10)
        second = load_baseline()

        assert second.accuracy >= first.accuracy


# ---------------------------------------------------------------------------
# load_baseline
# ---------------------------------------------------------------------------

class TestLoadBaseline:

    def test_returns_empty_when_no_file(self, tmp_baseline_path):
        b = load_baseline()
        assert b.sample_count == 0

    def test_loads_from_disk(self, tmp_baseline_path):
        baseline = RegressionBaseline(accuracy=0.77, sample_count=5)
        _save_baseline(baseline)
        loaded = load_baseline()
        assert loaded.accuracy == pytest.approx(0.77)
        assert loaded.sample_count == 5

    def test_handles_corrupt_file_gracefully(self, tmp_baseline_path):
        with open(tmp_baseline_path, "w") as f:
            f.write("not valid json {{{")
        b = load_baseline()
        assert b.sample_count == 0
