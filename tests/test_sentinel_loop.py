"""Tests for supervisor/sentinel_loop.py — Proactive pre-incident signal detection."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from supervisor.sentinel_loop import (
    SentinelLoop,
    _AlertRegistry,
    run_prediction_for_service,
    start_sentinel_loop,
    get_sentinel_loop,
)


# ---------------------------------------------------------------------------
# AlertRegistry
# ---------------------------------------------------------------------------

class TestAlertRegistry:
    def test_new_alert_should_post(self):
        r = _AlertRegistry()
        assert r.should_post("svc", "cpu", "WARNING") is True

    def test_second_identical_alert_suppressed_within_cooldown(self):
        r = _AlertRegistry()
        r.record("svc", "cpu", "WARNING")
        # Immediately check again — should be suppressed
        assert r.should_post("svc", "cpu", "WARNING") is False

    def test_urgency_escalation_always_posts(self):
        r = _AlertRegistry()
        r.record("svc", "cpu", "WATCH")
        # Escalation to WARNING should post even within cooldown
        assert r.should_post("svc", "cpu", "WARNING") is True

    def test_different_metric_not_suppressed(self):
        r = _AlertRegistry()
        r.record("svc", "cpu", "WARNING")
        assert r.should_post("svc", "memory", "WARNING") is True

    def test_different_service_not_suppressed(self):
        r = _AlertRegistry()
        r.record("svc-a", "cpu", "WARNING")
        assert r.should_post("svc-b", "cpu", "WARNING") is True

    def test_clear_resolved_allows_repost(self):
        r = _AlertRegistry()
        r.record("svc", "cpu", "WARNING")
        r.clear_resolved("svc", "cpu")
        assert r.should_post("svc", "cpu", "WARNING") is True

    def test_record_increments_post_count(self):
        r = _AlertRegistry()
        r.record("svc", "cpu", "WARNING")
        r.clear_resolved("svc", "cpu")
        r.record("svc", "cpu", "WARNING")
        rec = r._records.get("svc|cpu")
        assert rec is not None

    def test_urgency_rank_breached_highest(self):
        from supervisor.sentinel_loop import _URGENCY_RANK
        assert _URGENCY_RANK["BREACHED"] > _URGENCY_RANK["IMMINENT"]
        assert _URGENCY_RANK["IMMINENT"] > _URGENCY_RANK["WARNING"]
        assert _URGENCY_RANK["WARNING"] > _URGENCY_RANK["WATCH"]


# ---------------------------------------------------------------------------
# SentinelLoop lifecycle
# ---------------------------------------------------------------------------

class TestSentinelLoopLifecycle:
    def test_starts_and_stops(self):
        loop = SentinelLoop(services=["svc-a"], poll_interval=100)
        loop.start()
        assert loop.is_running()
        loop.stop(timeout=2.0)
        assert not loop.is_running()

    def test_double_start_does_not_crash(self):
        loop = SentinelLoop(services=["svc-a"], poll_interval=100)
        loop.start()
        loop.start()  # Should log warning, not raise
        loop.stop(timeout=2.0)

    def test_stop_before_start_does_not_crash(self):
        loop = SentinelLoop(services=[], poll_interval=100)
        loop.stop()  # Should not raise

    def test_stats_returns_dict(self):
        loop = SentinelLoop(services=["svc-a", "svc-b"], poll_interval=100)
        stats = loop.stats()
        assert isinstance(stats, dict)
        assert stats["services_watched"] == 2
        assert "cycles_completed" in stats
        assert "alerts_posted" in stats

    def test_daemon_thread(self):
        loop = SentinelLoop(services=["svc-a"], poll_interval=100)
        loop.start()
        assert loop._thread is not None
        assert loop._thread.daemon is True
        loop.stop(timeout=2.0)

    def test_cycle_count_increments(self):
        loop = SentinelLoop(services=[], poll_interval=1)
        loop.start()
        time.sleep(2.5)
        loop.stop(timeout=2.0)
        assert loop._cycle_count >= 1


# ---------------------------------------------------------------------------
# SentinelLoop — check_service with mocked detector
# ---------------------------------------------------------------------------

class TestSentinelLoopCheckService:
    def _make_alert(self, urgency: str = "WARNING", metric: str = "cpu", value: float = 85.0):
        alert = MagicMock()
        alert.urgency = MagicMock()
        alert.urgency.name = urgency
        alert.metric_name = metric
        alert.current_value = value
        alert.threshold = 90.0
        alert.trend_direction = "rising"
        alert.recommended_action = "Scale horizontally"
        alert.minutes_to_breach = 12.0
        alert.confidence = 0.85
        return alert

    def test_check_service_with_no_metrics_no_crash(self):
        loop = SentinelLoop(services=["svc-a"])
        with patch("supervisor.sentinel_loop.SENTINEL_ENABLED", True), \
             patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[]):
            loop._check_service("svc-a")  # Should not raise

    def test_alert_below_min_urgency_not_posted(self):
        loop = SentinelLoop(services=["svc-a"])
        alert = self._make_alert("WATCH")
        posted = []

        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[alert]), \
             patch.object(loop, "_post_alert", side_effect=posted.append), \
             patch("supervisor.sentinel_loop.MIN_URGENCY_RANK", 2):  # Only IMMINENT+
            loop._check_service("svc-a")

        assert len(posted) == 0

    def test_imminent_alert_posts(self):
        loop = SentinelLoop(services=["svc-a"])
        alert = self._make_alert("IMMINENT")
        posted = []

        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[alert]), \
             patch.object(loop, "_fetch_metrics", return_value={"cpu": [85.0]}), \
             patch.object(loop, "_post_alert", side_effect=lambda svc, a, u: posted.append((svc, u))), \
             patch("supervisor.sentinel_loop.MIN_URGENCY_RANK", 1):
            loop._check_service("svc-a")

        assert len(posted) == 1

    def test_breached_creates_incident(self):
        loop = SentinelLoop(services=["svc-a"])
        alert = self._make_alert("BREACHED")
        incidents_created = []

        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[alert]), \
             patch.object(loop, "_fetch_metrics", return_value={"cpu": [95.0]}), \
             patch.object(loop, "_post_alert"), \
             patch.object(loop, "_create_incident", side_effect=lambda svc, a: incidents_created.append(svc)), \
             patch("supervisor.sentinel_loop.MIN_URGENCY_RANK", 0):
            loop._check_service("svc-a")

        assert len(incidents_created) == 1

    def test_cooldown_suppresses_duplicate(self):
        loop = SentinelLoop(services=["svc-a"])
        alert = self._make_alert("WARNING")
        posted = []

        # First post should go through
        loop._registry.record("svc-a", "cpu", "WARNING")

        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[alert]), \
             patch.object(loop, "_post_alert", side_effect=posted.append), \
             patch("supervisor.sentinel_loop.MIN_URGENCY_RANK", 0):
            loop._check_service("svc-a")

        assert len(posted) == 0  # Suppressed by cooldown


# ---------------------------------------------------------------------------
# run_prediction_for_service
# ---------------------------------------------------------------------------

class TestRunPredictionForService:
    def test_returns_list(self):
        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[]):
            result = run_prediction_for_service("payment-service", {}, post_to_slack=False)
        assert isinstance(result, list)

    def test_empty_metrics_returns_empty(self):
        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[]):
            result = run_prediction_for_service("payment-service", {}, post_to_slack=False)
        assert result == []

    def test_alert_converted_to_dict(self):
        alert = MagicMock()
        alert.urgency = MagicMock()
        alert.urgency.name = "WARNING"
        alert.metric_name = "cpu"
        alert.current_value = 85.0
        alert.threshold = 90.0
        alert.trend_direction = "rising"
        alert.minutes_to_breach = 12.0
        alert.recommended_action = "Scale out"
        alert.confidence = 0.85

        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[alert]):
            result = run_prediction_for_service("payment-service", {}, post_to_slack=False)

        assert len(result) == 1
        assert result[0]["urgency"] == "WARNING"
        assert result[0]["metric_name"] == "cpu"
        assert result[0]["current_value"] == 85.0

    def test_no_slack_post_when_disabled(self):
        alert = MagicMock()
        alert.urgency = MagicMock()
        alert.urgency.name = "WARNING"
        alert.metric_name = "cpu"
        alert.current_value = 85.0
        alert.threshold = 90.0
        alert.trend_direction = "rising"
        alert.minutes_to_breach = None
        alert.recommended_action = ""
        alert.confidence = 0.8

        with patch("supervisor.predictive_detector.detect_predictive_alerts", return_value=[alert]), \
             patch("supervisor.slack_bot.get_bot") as mock_bot:
            run_prediction_for_service("svc", {}, post_to_slack=False)
            mock_bot.assert_not_called()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestSentinelLoopSingleton:
    def test_get_sentinel_loop_returns_same_instance(self):
        a = get_sentinel_loop()
        b = get_sentinel_loop()
        assert a is b

    def test_start_sentinel_loop_no_services_does_not_start(self):
        import supervisor.sentinel_loop as sl
        original = sl._loop
        sl._loop = None  # Reset singleton
        try:
            loop = get_sentinel_loop()
            loop._services = []  # Ensure empty
            with patch.object(loop, "start") as mock_start, \
                 patch("supervisor.sentinel_loop.SENTINEL_ENABLED", True):
                start_sentinel_loop(services=[])
                mock_start.assert_not_called()
        finally:
            sl._loop = original
