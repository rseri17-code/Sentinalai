"""Tests for the investigation metrics dashboard."""
from __future__ import annotations

import pytest

from supervisor.metrics_dashboard import (
    MetricsDashboard,
    InvestigationOutcome,
    DashboardSnapshot,
    record_investigation_outcome,
    get_dashboard,
    get_dashboard_engine,
    _safe_mean,
    _safe_percentile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _outcome(
    investigation_id="inv-001",
    incident_type="error_spike",
    service="payment-service",
    root_cause="Connection pool exhausted",
    confidence=85.0,
    severity=2,
    elapsed_ms=12000.0,
    tool_calls=10,
    fix_proposed=False,
    fix_applied=False,
    fix_verified=False,
):
    return InvestigationOutcome(
        investigation_id=investigation_id,
        incident_id=f"INC_{investigation_id}",
        incident_type=incident_type,
        service=service,
        root_cause=root_cause,
        confidence=confidence,
        severity=severity,
        elapsed_ms=elapsed_ms,
        tool_calls=tool_calls,
        llm_input_tokens=3000,
        llm_output_tokens=500,
        citation_coverage=0.8,
        fix_proposed=fix_proposed,
        fix_applied=fix_applied,
        fix_verified=fix_verified,
    )


# ---------------------------------------------------------------------------
# InvestigationOutcome
# ---------------------------------------------------------------------------

class TestInvestigationOutcome:
    def test_has_root_cause_true_when_rc_set(self):
        o = _outcome(root_cause="Connection pool exhausted")
        assert o.has_root_cause is True

    def test_has_root_cause_false_when_empty(self):
        o = _outcome(root_cause="")
        assert o.has_root_cause is False

    def test_has_root_cause_false_when_unknown(self):
        o = _outcome(root_cause="UNKNOWN")
        assert o.has_root_cause is False

    def test_has_root_cause_false_when_undetermined(self):
        o = _outcome(root_cause="UNDETERMINED")
        assert o.has_root_cause is False

    def test_llm_total_tokens_sums_both(self):
        o = _outcome()
        assert o.llm_total_tokens == 3000 + 500


# ---------------------------------------------------------------------------
# MetricsDashboard — record + snapshot
# ---------------------------------------------------------------------------

class TestMetricsDashboard:
    def test_empty_snapshot_on_no_records(self):
        d = MetricsDashboard()
        snap = d.get_dashboard()
        assert snap.total_investigations == 0

    def test_records_investigation(self):
        d = MetricsDashboard()
        d.record(_outcome())
        snap = d.get_dashboard()
        assert snap.total_investigations == 1

    def test_ring_buffer_evicts_oldest(self):
        d = MetricsDashboard(ring_size=3)
        for i in range(5):
            d.record(_outcome(investigation_id=f"inv-{i}"))
        snap = d.get_dashboard()
        assert snap.total_investigations == 3

    def test_mttr_computed_from_elapsed(self):
        d = MetricsDashboard()
        d.record(_outcome(elapsed_ms=10000.0))
        d.record(_outcome(elapsed_ms=20000.0))
        snap = d.get_dashboard()
        # median of [10000, 20000] should be in between
        assert 10000 <= snap.mttr_median_ms <= 20000

    def test_mean_confidence(self):
        d = MetricsDashboard()
        d.record(_outcome(confidence=80.0))
        d.record(_outcome(confidence=60.0))
        snap = d.get_dashboard()
        assert snap.mean_confidence == 70.0

    def test_root_cause_found_rate(self):
        d = MetricsDashboard()
        d.record(_outcome(root_cause="Connection pool exhausted"))
        d.record(_outcome(root_cause=""))    # no root cause
        snap = d.get_dashboard()
        assert snap.root_cause_found_rate == 0.5

    def test_false_positive_rate(self):
        d = MetricsDashboard()
        d.record(_outcome(confidence=25.0))  # FP (< 30)
        d.record(_outcome(confidence=85.0))
        snap = d.get_dashboard()
        assert snap.false_positive_rate == 0.5

    def test_fix_proposed_rate(self):
        d = MetricsDashboard()
        d.record(_outcome(fix_proposed=True))
        d.record(_outcome(fix_proposed=False))
        d.record(_outcome(fix_proposed=True))
        snap = d.get_dashboard()
        assert snap.fix_proposed_rate == pytest.approx(2/3, 0.01)

    def test_by_incident_type_groups_correctly(self):
        d = MetricsDashboard()
        d.record(_outcome(incident_type="error_spike", confidence=80.0))
        d.record(_outcome(incident_type="error_spike", confidence=60.0))
        d.record(_outcome(incident_type="latency", confidence=70.0))
        snap = d.get_dashboard()
        assert "error_spike" in snap.by_incident_type
        assert "latency" in snap.by_incident_type
        assert snap.by_incident_type["error_spike"]["count"] == 2

    def test_by_severity_groups_correctly(self):
        d = MetricsDashboard()
        d.record(_outcome(severity=1))
        d.record(_outcome(severity=2))
        d.record(_outcome(severity=2))
        snap = d.get_dashboard()
        assert "1" in snap.by_severity
        assert "2" in snap.by_severity
        assert snap.by_severity["2"]["count"] == 2

    def test_last_24h_count(self):
        d = MetricsDashboard()
        # Recent outcomes
        d.record(_outcome(investigation_id="recent-1"))
        d.record(_outcome(investigation_id="recent-2"))
        snap = d.get_dashboard()
        assert snap.last_24h_count == 2

    def test_snapshot_to_dict_has_all_keys(self):
        d = MetricsDashboard()
        d.record(_outcome())
        snap = d.get_dashboard()
        d_dict = snap.to_dict()
        assert "total_investigations" in d_dict
        assert "mttr_median_ms" in d_dict
        assert "mean_confidence" in d_dict
        assert "root_cause_found_rate" in d_dict
        assert "fix_proposed_rate" in d_dict
        assert "by_incident_type" in d_dict
        assert "generated_at_iso" in d_dict


# ---------------------------------------------------------------------------
# Calibration curve
# ---------------------------------------------------------------------------

class TestCalibrationCurve:
    def test_returns_empty_list_when_no_data(self):
        d = MetricsDashboard()
        curve = d.get_calibration_curve()
        assert curve == []

    def test_returns_correct_bucket_count(self):
        d = MetricsDashboard()
        for i in range(20):
            d.record(_outcome(confidence=float(i * 5)))
        curve = d.get_calibration_curve(buckets=5)
        assert len(curve) <= 5

    def test_bucket_has_required_fields(self):
        d = MetricsDashboard()
        d.record(_outcome(confidence=75.0, root_cause="something real"))
        curve = d.get_calibration_curve()
        for bucket in curve:
            assert "bucket_min" in bucket
            assert "bucket_max" in bucket
            assert "predicted_mean" in bucket
            assert "actual_correct_rate" in bucket
            assert "count" in bucket

    def test_actual_correct_rate_between_0_and_1(self):
        d = MetricsDashboard()
        for c in [30, 50, 70, 90]:
            d.record(_outcome(confidence=float(c), root_cause="cause found"))
        curve = d.get_calibration_curve()
        for bucket in curve:
            assert 0.0 <= bucket["actual_correct_rate"] <= 1.0


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------

class TestTrend:
    def test_returns_buckets_even_with_no_data(self):
        """Trend always returns the correct number of buckets for chart rendering."""
        d = MetricsDashboard()
        trend = d.get_trend(window_hours=4, resolution_hours=1)
        assert len(trend) == 4
        # All counts should be zero
        assert all(b["count"] == 0 for b in trend)

    def test_returns_correct_bucket_structure(self):
        d = MetricsDashboard()
        d.record(_outcome())
        trend = d.get_trend(window_hours=24, resolution_hours=1)
        for bucket in trend:
            assert "ts" in bucket
            assert "count" in bucket
            assert "mean_confidence" in bucket
            assert "mean_elapsed_ms" in bucket
            assert "root_cause_found" in bucket

    def test_trend_has_correct_num_buckets(self):
        d = MetricsDashboard()
        trend = d.get_trend(window_hours=4, resolution_hours=1)
        assert len(trend) == 4


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_safe_mean_empty(self):
        assert _safe_mean([]) == 0.0

    def test_safe_mean_values(self):
        assert _safe_mean([10.0, 20.0]) == 15.0

    def test_safe_percentile_empty(self):
        assert _safe_percentile([], 50) == 0.0

    def test_safe_percentile_median(self):
        result = _safe_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50)
        assert result == 3.0

    def test_safe_percentile_p95(self):
        values = list(range(1, 101))
        result = _safe_percentile([float(v) for v in values], 95)
        assert result >= 90


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

class TestModuleLevelFunctions:
    def test_record_investigation_outcome_does_not_raise(self):
        # Should not raise even with minimal args
        record_investigation_outcome(
            investigation_id="test-inv",
            incident_id="INC001",
        )

    def test_get_dashboard_returns_snapshot(self):
        snap = get_dashboard()
        assert isinstance(snap, DashboardSnapshot)

    def test_get_dashboard_engine_returns_same_instance(self):
        e1 = get_dashboard_engine()
        e2 = get_dashboard_engine()
        assert e1 is e2
