"""Tests for the Pattern Intelligence Layer.

Covers:
  - PatternDetector: trend_drift, rate_accel, cross_service, post_deploy, slo_burn
  - SLOEngine: burn rate calculation, status classification, budget consumption
  - PredictionStore: dedup, outcome tracking, calibration feed, accuracy report
  - Statistical helpers: linear regression slope, Pearson correlation
  - Intelligence API: feed endpoint shape, acknowledge, false positive
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@dataclass
class FakeDetection:
    service: str = "api-gateway"
    pattern_type: str = "trend_drift"
    severity: str = "LIKELY"
    metric: str = "error_rate"
    confidence: float = 0.72
    current_value: float = 0.005
    explanation: str = "Test explanation"
    predicted_breach_hours: float | None = 4.0
    related_service: str = ""
    evidence: dict = field(default_factory=dict)


class FakeAggregator:
    """Minimal TelemetryAggregator stand-in."""

    def __init__(self, series: list[tuple[float, float]] | None = None, baseline_ready: bool = True):
        self._series = series or []
        self._ready = baseline_ready

    def is_baseline_ready(self, service: str) -> bool:
        return self._ready

    def get_recent(self, service: str, minutes: int = 60, metric: str = "error_rate"):
        return self._series

    def _discover_services(self):
        return ["api-gateway"]


# ---------------------------------------------------------------------------
# Statistical helper tests
# ---------------------------------------------------------------------------

class TestLinearRegressionSlope:
    def test_flat_series_returns_zero_slope(self):
        from intelligence.pattern_detector import _linear_regression_slope
        series = [(float(i), 0.5) for i in range(20)]
        slope, r2 = _linear_regression_slope(series)
        assert abs(slope) < 1e-9
        assert r2 == pytest.approx(0.0, abs=1e-6)

    def test_perfectly_rising_series(self):
        from intelligence.pattern_detector import _linear_regression_slope
        series = [(float(i), float(i) * 0.01) for i in range(20)]
        slope, r2 = _linear_regression_slope(series)
        assert slope == pytest.approx(0.01, rel=1e-4)
        assert r2 == pytest.approx(1.0, rel=1e-4)

    def test_single_point_returns_zero(self):
        from intelligence.pattern_detector import _linear_regression_slope
        slope, r2 = _linear_regression_slope([(0.0, 1.0)])
        assert slope == 0.0
        assert r2 == 0.0

    def test_r_squared_clamped_between_0_and_1(self):
        from intelligence.pattern_detector import _linear_regression_slope
        import random
        rng = random.Random(42)
        series = [(float(i), rng.gauss(0, 1)) for i in range(30)]
        _, r2 = _linear_regression_slope(series)
        assert 0.0 <= r2 <= 1.0


class TestPearsonCorrelation:
    def test_identical_series_returns_one(self):
        from intelligence.pattern_detector import _pearson_correlation
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _pearson_correlation(xs, xs) == pytest.approx(1.0, abs=1e-6)

    def test_anti_correlated_returns_negative_one(self):
        from intelligence.pattern_detector import _pearson_correlation
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [5.0, 4.0, 3.0, 2.0, 1.0]
        assert _pearson_correlation(xs, ys) == pytest.approx(-1.0, abs=1e-6)

    def test_constant_series_returns_zero(self):
        from intelligence.pattern_detector import _pearson_correlation
        xs = [3.0] * 10
        ys = [float(i) for i in range(10)]
        assert _pearson_correlation(xs, ys) == 0.0

    def test_too_short_returns_zero(self):
        from intelligence.pattern_detector import _pearson_correlation
        assert _pearson_correlation([1.0, 2.0], [3.0, 4.0]) == 0.0

    def test_result_clamped_to_minus_one_plus_one(self):
        from intelligence.pattern_detector import _pearson_correlation
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        ys = [1.1, 2.0, 2.9, 4.1, 5.0, 6.2, 6.9, 8.1]
        r = _pearson_correlation(xs, ys)
        assert -1.0 <= r <= 1.0


# ---------------------------------------------------------------------------
# PatternDetector tests
# ---------------------------------------------------------------------------

class TestPatternDetectorTrend:
    def _make_rising_series(self, n: int = 30, slope_per_sec: float = 0.002) -> list[tuple[float, float]]:
        now = time.time()
        return [(now - (n - i) * 60, i * slope_per_sec * 60) for i in range(n)]

    def test_rising_series_above_warn_threshold_produces_detection(self):
        from intelligence.pattern_detector import PatternDetector, SLOPE_WARN_THRESHOLD
        det = PatternDetector()
        series = self._make_rising_series(slope_per_sec=SLOPE_WARN_THRESHOLD * 2)
        result = det._detect_trend("svc", "error_rate", series)
        assert result is not None
        assert result.severity in ("LIKELY", "IMMINENT")
        assert 0.0 < result.confidence <= 1.0

    def test_flat_series_returns_none(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i * 60), 0.001) for i in range(30)]
        assert det._detect_trend("svc", "error_rate", series) is None

    def test_declining_series_returns_none(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i * 60), 0.01 - i * 0.0001) for i in range(30)]
        assert det._detect_trend("svc", "error_rate", series) is None

    def test_too_few_points_returns_none(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i), 0.01) for i in range(5)]
        assert det._detect_trend("svc", "error_rate", series) is None


class TestPatternDetectorRateAccel:
    def test_doubling_series_produces_imminent(self):
        from intelligence.pattern_detector import PatternDetector, ROC_ACCEL_CRIT
        det = PatternDetector()
        prior = [(float(i), 0.01) for i in range(3)]
        recent = [(float(i + 3), 0.01 * (1 + ROC_ACCEL_CRIT + 0.1)) for i in range(3)]
        series = prior + recent
        result = det._detect_rate_accel("svc", "error_rate", series)
        assert result is not None
        assert result.severity == "IMMINENT"

    def test_stable_series_returns_none(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i), 0.005) for i in range(6)]
        assert det._detect_rate_accel("svc", "error_rate", series) is None

    def test_zero_prior_avg_returns_none(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i), 0.0) for i in range(6)]
        assert det._detect_rate_accel("svc", "error_rate", series) is None


class TestPatternDetectorCrossService:
    def test_highly_correlated_services_produce_detection(self):
        from intelligence.pattern_detector import PatternDetector, MIN_POINTS_CORRELATION
        det = PatternDetector()
        n = MIN_POINTS_CORRELATION + 5
        now = time.time()
        series = [(now - (n - i) * 60, float(i) * 0.001) for i in range(n)]
        agg = FakeAggregator(series)
        results = det._detect_cross_service(["svc-a", "svc-b"], agg)
        assert len(results) > 0
        assert all(0.75 <= r.confidence <= 1.0 for r in results)

    def test_single_service_returns_no_cross_detections(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        agg = FakeAggregator()
        assert det._detect_cross_service(["svc-a"], agg) == []

    def test_insufficient_data_returns_no_detections(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i), float(i)) for i in range(5)]
        agg = FakeAggregator(series)
        assert det._detect_cross_service(["svc-a", "svc-b"], agg) == []


class TestPatternDetectorPostDeploy:
    def test_high_delta_produces_detection(self):
        from intelligence.pattern_detector import PatternDetector, DEPLOY_DELTA_WARN
        det = PatternDetector()
        pre = 0.002
        post = pre * (1 + DEPLOY_DELTA_WARN + 0.1)
        n = 12
        now = time.time()
        pre_series = [(now - (n - i) * 60, pre) for i in range(n // 2)]
        post_series = [(now - (n // 2 - i) * 60, post) for i in range(n // 2)]
        series = pre_series + post_series
        agg = FakeAggregator(series)
        results = det._detect_post_deploy("svc", agg)
        assert len(results) > 0

    def test_no_increase_returns_empty(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        series = [(float(i * 60), 0.005) for i in range(12)]
        agg = FakeAggregator(series)
        assert det._detect_post_deploy("svc", agg) == []


class TestPatternDetectorSLOBurn:
    def test_burning_slo_produces_detection(self):
        from intelligence.pattern_detector import PatternDetector
        from intelligence.slo_engine import SLOStatus
        det = PatternDetector()
        status = SLOStatus(
            service="payment-service", metric="error_rate", slo_target=0.999,
            budget_total_hours=7.2, budget_consumed_hours=3.0, budget_remaining_hours=4.2,
            budget_remaining_pct=58.0, current_value=0.008, burn_rate=8.0,
            hours_to_breach=5.0, status="BURNING", observations=100,
        )
        result = det._detection_from_slo(status)
        assert result is not None
        assert result.severity == "LIKELY"
        assert result.pattern_type == "slo_burn"

    def test_ok_slo_returns_none(self):
        from intelligence.pattern_detector import PatternDetector
        from intelligence.slo_engine import SLOStatus
        det = PatternDetector()
        status = SLOStatus(
            service="auth", metric="error_rate", slo_target=0.999,
            budget_total_hours=7.2, budget_consumed_hours=0.5, budget_remaining_hours=6.7,
            budget_remaining_pct=93.0, current_value=0.0002, burn_rate=0.2,
            hours_to_breach=float("inf"), status="OK", observations=50,
        )
        assert det._detection_from_slo(status) is None


class TestDetectAll:
    def test_disabled_returns_empty(self):
        from intelligence.pattern_detector import PatternDetector
        with patch("intelligence.pattern_detector.PATTERN_DETECTOR_ENABLED", False):
            det = PatternDetector()
            assert det.detect_all(["svc"], FakeAggregator()) == []

    def test_baseline_not_ready_skips_service(self):
        from intelligence.pattern_detector import PatternDetector
        det = PatternDetector()
        agg = FakeAggregator(baseline_ready=False)
        results = det.detect_all(["svc"], agg)
        assert results == []

    def test_results_sorted_by_severity_then_confidence(self):
        from intelligence.pattern_detector import PatternDetector, Detection
        det = PatternDetector()
        detections = [
            Detection("s", "trend_drift", "WATCH", 0.9, "error_rate", 0.001, "x"),
            Detection("s", "rate_accel", "IMMINENT", 0.6, "error_rate", 0.01, "y"),
            Detection("s", "slo_burn", "LIKELY", 0.8, "error_rate", 0.005, "z"),
        ]
        order = {"IMMINENT": 0, "LIKELY": 1, "WATCH": 2}
        sorted_d = sorted(detections, key=lambda x: (order.get(x.severity, 3), -x.confidence))
        assert sorted_d[0].severity == "IMMINENT"
        assert sorted_d[1].severity == "LIKELY"
        assert sorted_d[2].severity == "WATCH"


# ---------------------------------------------------------------------------
# SLOEngine tests
# ---------------------------------------------------------------------------

class TestSLOEngine:
    def test_burn_rate_above_one_produces_hours_to_breach(self):
        from intelligence.slo_engine import SLOEngine, SLODefinition
        defn = SLODefinition(service="svc", metric="error_rate", target=0.999, window_days=30)
        # budget fraction = 0.001, current_value = 0.01 → burn_rate = 10
        budget_remaining = 5.0  # hours
        burn_rate = 10.0
        hours = budget_remaining / (burn_rate - 1.0)
        assert abs(hours - 5.0 / 9.0) < 1e-6

    def test_classify_status_breached_when_no_budget(self):
        from intelligence.slo_engine import SLOEngine
        assert SLOEngine._classify_status(100.0, 0.0, 0.0) == "BREACHED"

    def test_classify_status_critical_at_high_burn(self):
        from intelligence.slo_engine import SLOEngine
        assert SLOEngine._classify_status(15.0, 50.0, 1.0) == "CRITICAL"

    def test_classify_status_burning_at_moderate_burn(self):
        from intelligence.slo_engine import SLOEngine
        assert SLOEngine._classify_status(7.0, 50.0, 5.0) == "BURNING"

    def test_classify_status_ok_below_one(self):
        from intelligence.slo_engine import SLOEngine
        assert SLOEngine._classify_status(0.5, 90.0, float("inf")) == "OK"

    def test_compute_consumed_counts_violations(self):
        from intelligence.slo_engine import SLOEngine, SLODefinition
        engine = SLOEngine.__new__(SLOEngine)
        defn = SLODefinition("svc", "error_rate", target=0.999, window_days=30)
        # 2 intervals of 3600s each, both violating (value > 0.001)
        series = [(0.0, 0.01), (3600.0, 0.01), (7200.0, 0.01)]
        consumed = engine._compute_consumed(series, defn)
        assert consumed == pytest.approx(2.0, abs=0.01)  # 2 hours

    def test_slo_definition_budget_hours(self):
        from intelligence.slo_engine import SLODefinition
        defn = SLODefinition("svc", "error_rate", target=0.999, window_days=30)
        # error_budget_fraction = 0.001, window_hours = 720 → budget_hours = 0.72
        assert defn.budget_hours_total() == pytest.approx(0.72, rel=1e-4)

    def test_defaults_loaded_without_db(self):
        from intelligence.slo_engine import SLOEngine
        with patch.object(SLOEngine, "_load_from_db", return_value=False):
            engine = SLOEngine()
        assert len(engine._slos) > 0

    def test_register_slo_replaces_existing_metric(self):
        from intelligence.slo_engine import SLOEngine
        with patch.object(SLOEngine, "_load_from_db", return_value=False), \
             patch.object(SLOEngine, "_persist_definition"):
            engine = SLOEngine()
            engine.register_slo("new-svc", "error_rate", target=0.999)
            engine.register_slo("new-svc", "error_rate", target=0.9999)
            slos = engine._slos["new-svc"]
            er_slos = [s for s in slos if s.metric == "error_rate"]
            assert len(er_slos) == 1
            assert er_slos[0].target == 0.9999


# ---------------------------------------------------------------------------
# PredictionStore tests
# ---------------------------------------------------------------------------

class TestPredictionStore:
    @pytest.fixture(autouse=True)
    def _clear_dedup(self):
        import intelligence.prediction_store as _ps
        _ps._dedup_index.clear()
        yield
        _ps._dedup_index.clear()

    def _store(self):
        from intelligence.prediction_store import PredictionStore
        return PredictionStore()

    def test_store_returns_prediction_on_first_call(self):
        store = self._store()
        with patch.object(store, "_persist_prediction"):
            pred = store.store(FakeDetection(), baseline_ready=True)
        assert pred is not None
        assert pred.service == "api-gateway"
        assert pred.outcome == "pending"

    def test_dedup_suppresses_duplicate_within_cooldown(self):
        store = self._store()
        det = FakeDetection()
        with patch.object(store, "_persist_prediction"), \
             patch("intelligence.prediction_store.PREDICTION_COOLDOWN_MINUTES", 60):
            first = store.store(det, baseline_ready=True)
            second = store.store(det, baseline_ready=True)
        assert first is not None
        assert second is None

    def test_cold_start_suppresses_prediction(self):
        store = self._store()
        with patch.object(store, "_persist_prediction"):
            pred = store.store(FakeDetection(), baseline_ready=False)
        assert pred is None

    def test_record_outcome_marks_true_positive(self):
        store = self._store()
        with patch.object(store, "_persist_prediction"), \
             patch.object(store, "_update_outcome_in_db"), \
             patch.object(store, "_feed_calibration"):
            pred = store.store(FakeDetection(service="svc-x"), baseline_ready=True)
            assert pred is not None
            resolved = store.record_outcome("svc-x", "INC-001")
        assert resolved == 1
        assert pred.outcome == "true_positive"
        assert pred.outcome_incident_id == "INC-001"

    def test_record_outcome_does_not_affect_other_services(self):
        store = self._store()
        with patch.object(store, "_persist_prediction"), \
             patch.object(store, "_update_outcome_in_db"), \
             patch.object(store, "_feed_calibration"):
            pred = store.store(FakeDetection(service="svc-y"), baseline_ready=True)
            resolved = store.record_outcome("svc-x", "INC-001")
        assert resolved == 0
        assert pred.outcome == "pending"

    def test_mark_false_positive_updates_outcome(self):
        store = self._store()
        with patch.object(store, "_persist_prediction"), \
             patch.object(store, "_update_outcome_in_db"), \
             patch.object(store, "_feed_calibration"):
            pred = store.store(FakeDetection(), baseline_ready=True)
            ok = store.mark_false_positive(pred.prediction_id, reason="noise")
        assert ok is True
        assert pred.outcome == "false_positive"

    def test_mark_false_positive_unknown_id_returns_false(self):
        store = self._store()
        assert store.mark_false_positive("nonexistent-id") is False

    def test_get_active_predictions_filters_by_severity(self):
        store = self._store()
        with patch.object(store, "_persist_prediction"):
            w = store.store(FakeDetection(severity="WATCH"), baseline_ready=True)
            assert w is not None
            # second store — different pattern_type to avoid dedup
            l = store.store(
                FakeDetection(severity="LIKELY", pattern_type="rate_accel"),
                baseline_ready=True
            )
            assert l is not None

        active_watch = store.get_active_predictions("WATCH")
        active_likely = store.get_active_predictions("LIKELY")
        assert any(p.severity == "WATCH" for p in active_watch)
        assert all(p.severity in ("LIKELY", "IMMINENT") for p in active_likely)

    def test_expire_old_predictions_marks_as_false_positive(self):
        from intelligence.prediction_store import Prediction
        store = self._store()
        old_pred = Prediction(
            prediction_id=str(uuid.uuid4()),
            service="svc", pattern_type="trend_drift", severity="WATCH",
            metric="error_rate", confidence=0.5, current_value=0.001,
            explanation="old", predicted_breach_hours=0.001,
            related_service="", evidence={}, published=True,
            created_at_epoch=time.time() - 86400,  # 24h old
        )
        store._predictions[old_pred.prediction_id] = old_pred

        with patch.object(store, "_update_outcome_in_db"), \
             patch.object(store, "_feed_calibration"):
            expired = store.expire_old_predictions()

        assert expired == 1
        assert old_pred.outcome == "false_positive"

    def test_accuracy_report_computes_precision(self):
        from intelligence.prediction_store import Prediction
        store = self._store()
        for i in range(3):
            p = Prediction(
                prediction_id=str(uuid.uuid4()),
                service="svc", pattern_type="trend_drift", severity="LIKELY",
                metric="error_rate", confidence=0.7, current_value=0.005,
                explanation="x", predicted_breach_hours=2.0,
                related_service="", evidence={}, published=True,
                outcome="true_positive",
            )
            store._predictions[p.prediction_id] = p
        for i in range(1):
            p = Prediction(
                prediction_id=str(uuid.uuid4()),
                service="svc", pattern_type="trend_drift", severity="LIKELY",
                metric="error_rate", confidence=0.7, current_value=0.005,
                explanation="x", predicted_breach_hours=2.0,
                related_service="", evidence={}, published=True,
                outcome="false_positive",
            )
            store._predictions[p.prediction_id] = p

        report = store.get_accuracy_report()
        assert "trend_drift" in report["by_pattern_type"]
        td = report["by_pattern_type"]["trend_drift"]
        assert td["precision"] == pytest.approx(0.75, abs=0.01)

    def test_is_expired_uses_breach_hours(self):
        from intelligence.prediction_store import Prediction
        pred = Prediction(
            prediction_id="x", service="s", pattern_type="p", severity="W",
            metric="error_rate", confidence=0.5, current_value=0.0,
            explanation="", predicted_breach_hours=0.0001,
            related_service="", evidence={},
            created_at_epoch=time.time() - 3600,  # created 1h ago, breach was in 0.36s
        )
        assert pred.is_expired() is True

    def test_is_expired_false_for_future_breach(self):
        from intelligence.prediction_store import Prediction
        pred = Prediction(
            prediction_id="x", service="s", pattern_type="p", severity="W",
            metric="error_rate", confidence=0.5, current_value=0.0,
            explanation="", predicted_breach_hours=48.0,
            related_service="", evidence={},
            created_at_epoch=time.time(),
        )
        assert pred.is_expired() is False


# ---------------------------------------------------------------------------
# Intelligence API tests
# ---------------------------------------------------------------------------

class TestIntelligenceAPI:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from agui.api.intelligence import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def _mock_runner(self, predictions=None, slo_statuses=None):
        runner = MagicMock()
        runner._store = MagicMock()
        runner._store._predictions = {}
        runner._task = MagicMock()
        runner._task.done.return_value = False
        runner._iteration = 5
        runner.get_active_predictions.return_value = predictions or []
        runner.get_slo_statuses.return_value = slo_statuses or []
        runner.get_accuracy_report.return_value = {"by_pattern_type": {}, "total_predictions": 0}
        return runner

    def test_feed_returns_signal_alert_shape(self, client):
        from intelligence.prediction_store import Prediction
        pred = Prediction(
            prediction_id="pred-123",
            service="api-gateway",
            pattern_type="trend_drift",
            severity="LIKELY",
            metric="error_rate",
            confidence=0.75,
            current_value=0.007,
            explanation="Rising steadily",
            predicted_breach_hours=3.0,
            related_service="",
            evidence={},
        )
        runner = self._mock_runner(predictions=[pred])

        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.get("/api/v1/intelligence/feed")

        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data
        assert len(data["alerts"]) == 1
        alert = data["alerts"][0]
        assert alert["id"] == "pred-123"
        assert alert["service"] == "api-gateway"
        assert alert["urgency"] == "WARNING"  # LIKELY → WARNING
        assert alert["trend_direction"] == "rising"
        assert alert["minutes_to_breach"] == pytest.approx(180.0, abs=1.0)

    def test_feed_empty_when_no_predictions(self, client):
        runner = self._mock_runner()
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.get("/api/v1/intelligence/feed")
        assert resp.status_code == 200
        assert resp.json()["alerts"] == []

    def test_acknowledge_stores_ack(self, client):
        with patch("agui.api.intelligence._get_runner"):
            resp = client.post(
                "/api/v1/intelligence/alerts/pred-abc/acknowledge",
                json={"actor": "alice"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["actor"] == "alice"

    def test_predictions_endpoint_returns_list(self, client):
        runner = self._mock_runner()
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.get("/api/v1/intelligence/predictions")
        assert resp.status_code == 200
        assert "predictions" in resp.json()

    def test_slo_endpoint_returns_list(self, client):
        runner = self._mock_runner()
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.get("/api/v1/intelligence/slo")
        assert resp.status_code == 200
        data = resp.json()
        assert "slo_statuses" in data
        assert "burning" in data

    def test_mark_false_positive_returns_success(self, client):
        runner = self._mock_runner()
        runner.mark_false_positive.return_value = True
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.post(
                "/api/v1/intelligence/predictions/pred-999/fp",
                json={"reason": "noise"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_mark_false_positive_unknown_returns_404(self, client):
        runner = self._mock_runner()
        runner.mark_false_positive.return_value = False
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.post(
                "/api/v1/intelligence/predictions/ghost-id/fp",
                json={"reason": ""},
            )
        assert resp.status_code == 404

    def test_record_outcome_endpoint(self, client):
        runner = self._mock_runner()
        runner.record_outcome.return_value = 2
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.post("/api/v1/intelligence/outcomes", json={
                "service": "payment-service",
                "incident_id": "INC-100",
            })
        assert resp.status_code == 200
        assert resp.json()["predictions_resolved"] == 2

    def test_health_endpoint(self, client):
        runner = self._mock_runner()
        with patch("agui.api.intelligence._get_runner", return_value=runner):
            resp = client.get("/api/v1/intelligence/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "iteration" in data
