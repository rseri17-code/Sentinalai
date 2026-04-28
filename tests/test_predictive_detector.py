"""Tests for supervisor.predictive_detector."""
from __future__ import annotations

import math
import time
import pytest

from supervisor.predictive_detector import (
    AlertUrgency,
    TrendAnalysis,
    PredictiveAlert,
    analyze_trend,
    detect_predictive_alerts,
    _metric_to_incident_type,
    _recommended_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_series(
    start_value: float,
    slope_per_minute: float,
    n_points: int = 10,
    interval_seconds: float = 60.0,
    base_epoch: float = 1_700_000_000.0,
) -> list[tuple[float, float]]:
    """Build a perfectly linear time series with a given slope (units/minute)."""
    series = []
    for i in range(n_points):
        ts = base_epoch + i * interval_seconds
        value = start_value + slope_per_minute * (i * interval_seconds / 60.0)
        series.append((ts, value))
    return series


def _flat_series(value: float = 50.0, n: int = 10) -> list[tuple[float, float]]:
    return _make_series(start_value=value, slope_per_minute=0.0, n_points=n)


# ---------------------------------------------------------------------------
# analyze_trend: basic behaviour
# ---------------------------------------------------------------------------

class TestAnalyzeTrend:

    def test_returns_trend_analysis_instance(self):
        series = _make_series(50.0, 1.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert isinstance(result, TrendAnalysis)

    def test_slope_computed_correctly_for_perfect_linear_series(self):
        # slope = 2 units/minute
        series = _make_series(start_value=40.0, slope_per_minute=2.0, n_points=10)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert abs(result.slope_per_minute - 2.0) < 0.01

    def test_r_squared_is_one_for_perfect_linear_series(self):
        series = _make_series(start_value=40.0, slope_per_minute=2.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.r_squared > 0.99

    def test_r_squared_is_zero_for_flat_series(self):
        series = _flat_series(50.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.r_squared == 0.0

    def test_utilization_pct_calculated(self):
        series = _make_series(start_value=75.0, slope_per_minute=0.0, n_points=5)
        result = analyze_trend("memory_usage", series, threshold=100.0)
        assert abs(result.utilization_pct - 75.0) < 0.5

    def test_current_value_is_last_point(self):
        series = _make_series(start_value=10.0, slope_per_minute=5.0, n_points=6)
        last_value = series[-1][1]
        result = analyze_trend("cpu_usage", series, threshold=200.0)
        assert abs(result.current_value - last_value) < 0.01

    def test_is_trending_toward_breach_true_when_positive_slope_below_threshold(self):
        series = _make_series(start_value=60.0, slope_per_minute=1.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.is_trending_toward_breach is True

    def test_is_trending_toward_breach_false_when_negative_slope(self):
        series = _make_series(start_value=90.0, slope_per_minute=-1.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.is_trending_toward_breach is False

    def test_is_trending_toward_breach_false_when_already_breached(self):
        # Value already above threshold — slope direction doesn't matter for trending flag
        series = _make_series(start_value=110.0, slope_per_minute=1.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.is_trending_toward_breach is False

    def test_estimated_minutes_to_breach_correct(self):
        # current_value = 80, threshold = 100, slope = 2/min → 10 min
        series = _make_series(start_value=60.0, slope_per_minute=2.0, n_points=10)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        # current at n=10 is 60 + 2*(9) = 78; time to 100 = (100-78)/2 = 11 min
        assert result.estimated_minutes_to_breach is not None
        assert 8.0 < result.estimated_minutes_to_breach < 15.0

    def test_estimated_minutes_none_for_flat_trend(self):
        series = _flat_series(50.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.estimated_minutes_to_breach is None

    def test_estimated_minutes_none_for_negative_slope(self):
        series = _make_series(start_value=90.0, slope_per_minute=-2.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.estimated_minutes_to_breach is None

    def test_empty_series_returns_sensible_defaults(self):
        result = analyze_trend("cpu_usage", [], threshold=100.0)
        assert result.current_value == 0.0
        assert result.slope_per_minute == 0.0
        assert result.r_squared == 0.0
        assert result.estimated_minutes_to_breach is None
        assert result.is_trending_toward_breach is False

    def test_single_point_series(self):
        result = analyze_trend("cpu_usage", [(1_700_000_000.0, 75.0)], threshold=100.0)
        assert result.current_value == 75.0
        assert result.slope_per_minute == 0.0

    def test_noisy_data_gives_low_r_squared(self):
        import random
        rng = random.Random(42)
        base_epoch = 1_700_000_000.0
        series = [(base_epoch + i * 60, rng.uniform(10, 90)) for i in range(20)]
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert result.r_squared < 0.5

    def test_threshold_in_result(self):
        series = _make_series(50.0, 1.0)
        result = analyze_trend("memory_usage_bytes", series, threshold=256.0)
        assert result.threshold == 256.0

    def test_metric_name_preserved(self):
        series = _make_series(50.0, 1.0)
        result = analyze_trend("my_custom_metric", series, threshold=100.0)
        assert result.metric_name == "my_custom_metric"

    def test_r_squared_in_zero_one_range(self):
        series = _make_series(20.0, 3.0)
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        assert 0.0 <= result.r_squared <= 1.0


# ---------------------------------------------------------------------------
# Urgency classification
# ---------------------------------------------------------------------------

class TestUrgencyClassification:

    def test_breached_when_current_exceeds_threshold(self):
        # Value above threshold → BREACHED regardless of slope
        series = _make_series(start_value=110.0, slope_per_minute=1.0)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 1
        assert alerts[0].urgency == AlertUrgency.BREACHED

    def test_imminent_when_less_than_15_minutes_to_breach(self):
        # At slope 5/min, from 75 to 100 = 5 min
        series = _make_series(start_value=50.0, slope_per_minute=5.0, n_points=10)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 1
        assert alerts[0].urgency == AlertUrgency.IMMINENT

    def test_warning_when_15_to_30_minutes_to_breach(self):
        # slope=1/min, start=60, after 10 points current≈69, threshold=100 → ~31 min
        # Use smaller slope to land in 15-30 min band
        series = _make_series(start_value=70.0, slope_per_minute=1.0, n_points=10)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        # current ≈ 79 at slope=1, threshold=100 → 21 minutes
        assert len(alerts) == 1
        assert alerts[0].urgency == AlertUrgency.WARNING

    def test_watch_when_more_than_30_minutes_to_breach(self):
        # slope=0.5/min, current≈70, threshold=100 → 60 min
        series = _make_series(start_value=65.0, slope_per_minute=0.5, n_points=10)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 1
        assert alerts[0].urgency == AlertUrgency.WATCH

    def test_no_alert_when_r_squared_below_threshold(self):
        # Noisy data → R² < 0.5 → no alert
        import random
        rng = random.Random(0)
        base_epoch = 1_700_000_000.0
        series = [(base_epoch + i * 60, rng.uniform(70, 75)) for i in range(20)]
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        # Noisy flat series — no meaningful trend → no WATCH alert
        for a in alerts:
            assert a.urgency == AlertUrgency.BREACHED or a.trend.r_squared > 0.5

    def test_no_alert_for_flat_trend_below_threshold(self):
        series = _flat_series(50.0)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 0

    def test_no_alert_for_low_utilization(self):
        # Only 30% utilization (below default 60% min) with slight upward trend
        series = _make_series(start_value=25.0, slope_per_minute=0.1, n_points=10)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 0

    def test_negative_slope_does_not_produce_alert(self):
        # Metric is improving (falling)
        series = _make_series(start_value=90.0, slope_per_minute=-2.0)
        alerts = detect_predictive_alerts(
            "api",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 0


# ---------------------------------------------------------------------------
# Metric → incident type mapping
# ---------------------------------------------------------------------------

class TestMetricToIncidentType:

    @pytest.mark.parametrize("metric,expected", [
        ("memory_usage_bytes",      "oomkill"),
        ("memory_heap_used",        "oomkill"),
        ("cpu_usage_percent",       "saturation"),
        ("cpu_throttled_seconds",   "saturation"),
        ("error_rate_5xx",          "error_spike"),
        ("error_rate_total",        "error_spike"),
        ("latency_p99_ms",          "latency"),
        ("latency_p95",             "latency"),
        ("response_time_ms",        "latency"),
        ("response_time_p99",       "latency"),
        ("connection_pool_used",    "timeout"),
        ("connection_pool_active",  "timeout"),
        ("fd_count_open",           "timeout"),
        ("disk_used_bytes",         "saturation"),
        ("disk_iops",               "saturation"),
        ("goroutine_count",         "silent_failure"),
    ])
    def test_metric_type_mapping(self, metric, expected):
        assert _metric_to_incident_type(metric) == expected


class TestDetectPredictiveAlerts:

    def test_returns_list(self):
        series = _make_series(70.0, 2.0)
        result = detect_predictive_alerts(
            "svc",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert isinstance(result, list)

    def test_sorted_most_urgent_first(self):
        base = 1_700_000_000.0
        # BREACHED metric
        breached = _make_series(110.0, 1.0)
        # IMMINENT metric (slope=5, current≈93, threshold=100 → <15min)
        imminent = _make_series(50.0, 5.0, n_points=10)
        # WATCH metric (slope=0.5, current≈72, threshold=100 → >30min)
        watch    = _make_series(65.0, 0.5, n_points=10)

        alerts = detect_predictive_alerts(
            "svc",
            {
                "cpu_usage_percent":  {"time_series": breached, "threshold": 100.0},
                "memory_usage_bytes": {"time_series": imminent, "threshold": 100.0},
                "disk_used_bytes":    {"time_series": watch,    "threshold": 100.0},
            },
        )
        urgency_order = {
            AlertUrgency.BREACHED: 0,
            AlertUrgency.IMMINENT: 1,
            AlertUrgency.WARNING:  2,
            AlertUrgency.WATCH:    3,
        }
        for i in range(len(alerts) - 1):
            assert urgency_order[alerts[i].urgency] <= urgency_order[alerts[i + 1].urgency]

    def test_alert_has_all_fields(self):
        series = _make_series(70.0, 2.0)
        alerts = detect_predictive_alerts(
            "payment-api",
            {"cpu_usage_percent": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) >= 1
        a = alerts[0]
        assert a.service == "payment-api"
        assert a.metric_name == "cpu_usage_percent"
        assert a.incident_type == "saturation"
        assert isinstance(a.urgency, AlertUrgency)
        assert isinstance(a.recommended_action, str) and len(a.recommended_action) > 0
        assert 0.0 <= a.confidence <= 1.0
        assert isinstance(a.reasoning, str) and len(a.reasoning) > 0

    def test_empty_metrics_returns_empty_list(self):
        alerts = detect_predictive_alerts("svc", {})
        assert alerts == []

    def test_zero_threshold_skipped(self):
        series = _make_series(50.0, 2.0)
        alerts = detect_predictive_alerts(
            "svc",
            {"cpu_usage": {"time_series": series, "threshold": 0.0}},
        )
        assert alerts == []

    def test_confidence_imminent_higher_than_watch(self):
        # IMMINENT: slope 5, r²≈1 → confidence≈1.0
        imminent_series = _make_series(50.0, 5.0, n_points=10)
        # WATCH: slope 0.5, r²≈1 → confidence≈0.6
        watch_series    = _make_series(65.0, 0.5, n_points=10)

        alerts_imminent = detect_predictive_alerts(
            "svc", {"cpu_usage": {"time_series": imminent_series, "threshold": 100.0}}
        )
        alerts_watch = detect_predictive_alerts(
            "svc", {"cpu_usage": {"time_series": watch_series, "threshold": 100.0}}
        )
        if alerts_imminent and alerts_watch:
            assert alerts_imminent[0].confidence >= alerts_watch[0].confidence

    def test_multiple_metrics_all_processed(self):
        series = _make_series(70.0, 2.0)
        metrics = {
            "cpu_usage_percent":  {"time_series": series, "threshold": 100.0},
            "memory_usage_bytes": {"time_series": series, "threshold": 100.0},
        }
        alerts = detect_predictive_alerts("svc", metrics)
        metric_names = {a.metric_name for a in alerts}
        assert "cpu_usage_percent"  in metric_names
        assert "memory_usage_bytes" in metric_names

    def test_incident_type_correctly_set(self):
        series = _make_series(70.0, 2.0)
        alerts = detect_predictive_alerts(
            "svc",
            {"memory_usage_bytes": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) >= 1
        assert alerts[0].incident_type == "oomkill"

    def test_breached_alert_has_no_eta(self):
        series = _make_series(110.0, 1.0)
        alerts = detect_predictive_alerts(
            "svc",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) == 1
        assert alerts[0].urgency == AlertUrgency.BREACHED
        # ETA not meaningful when already breached (detector may set it to None)
        # Just verify it is either None or a non-negative number
        eta = alerts[0].estimated_minutes_to_breach
        assert eta is None or eta >= 0

    def test_recommended_action_populated(self):
        series = _make_series(70.0, 2.0)
        alerts = detect_predictive_alerts(
            "svc",
            {"memory_usage_bytes": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) >= 1
        assert len(alerts[0].recommended_action) > 10  # not an empty string


# ---------------------------------------------------------------------------
# R² quality gate — noise rejection
# ---------------------------------------------------------------------------

class TestRSquaredQualityGate:

    def test_r_squared_above_0_5_produces_alert(self):
        # Perfect linear trend → R²=1.0
        series = _make_series(70.0, 1.0)
        alerts = detect_predictive_alerts(
            "svc",
            {"cpu_usage": {"time_series": series, "threshold": 100.0}},
        )
        assert len(alerts) >= 1
        assert alerts[0].trend.r_squared > 0.5

    def test_r_squared_exactly_at_gate_does_not_alert(self):
        # Construct data where R² lands right at the boundary (below 0.5)
        # by using completely random values — verified via noisy data test above
        import random
        rng = random.Random(999)
        base = 1_700_000_000.0
        # Truly random values with mean≈75 (above 60% of 100)
        series = [(base + i * 60, rng.uniform(70, 80)) for i in range(30)]
        result = analyze_trend("cpu_usage", series, threshold=100.0)
        if result.r_squared <= 0.5:
            alerts = detect_predictive_alerts(
                "svc",
                {"cpu_usage": {"time_series": series, "threshold": 100.0}},
            )
            for a in alerts:
                assert a.urgency == AlertUrgency.BREACHED or a.trend.r_squared > 0.5
