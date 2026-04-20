"""Tests for supervisor.incident_weather."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from supervisor.incident_weather import (
    RiskLevel,
    RiskFactor,
    ServiceForecast,
    WeatherForecast,
    generate_forecast,
    _score_to_risk_level,
    _overall_risk_level,
    _had_recent_incident,
    _generate_headline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_change(
    service: str = "payment-api",
    change_type: str = "deployment",
    scheduled_at: str | None = None,
    risk: str = "medium",
) -> dict:
    if scheduled_at is None:
        scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    return {
        "service":      service,
        "change_type":  change_type,
        "scheduled_at": scheduled_at,
        "risk":         risk,
    }


def _make_experience(
    service: str = "payment-api",
    incident_type: str = "error_spike",
    root_cause: str = "Bad deploy introduced null pointer",
    days_ago: int = 3,
    quality: float = 0.85,
) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "incident_id":          f"INC-{service}-{days_ago}",
        "incident_type":        incident_type,
        "service":              service,
        "root_cause":           root_cause,
        "evidence_keys":        ["logs", "metrics"],
        "confidence":           80,
        "online_quality_score": quality,
        "timestamp":            ts.isoformat(),
    }


def _make_health(
    error_rate: float = 0.0,
    latency_p95: float = 150.0,
    cpu: float = 50.0,
    memory: float = 60.0,
) -> dict:
    return {
        "error_rate":  error_rate,
        "latency_p95": latency_p95,
        "cpu":         cpu,
        "memory":      memory,
    }


# ---------------------------------------------------------------------------
# _score_to_risk_level
# ---------------------------------------------------------------------------

class TestScoreToRiskLevel:

    @pytest.mark.parametrize("score,expected", [
        (0.0,   RiskLevel.LOW),
        (10.0,  RiskLevel.LOW),
        (19.9,  RiskLevel.LOW),
        (20.0,  RiskLevel.MODERATE),
        (35.0,  RiskLevel.MODERATE),
        (50.0,  RiskLevel.HIGH),
        (74.9,  RiskLevel.HIGH),
        (75.0,  RiskLevel.SEVERE),
        (90.0,  RiskLevel.SEVERE),
        (100.0, RiskLevel.SEVERE),
    ])
    def test_risk_level_boundaries(self, score, expected):
        assert _score_to_risk_level(score) == expected


# ---------------------------------------------------------------------------
# Change type base risks
# ---------------------------------------------------------------------------

class TestChangeTypeBaseRisks:

    def test_database_migration_base_risk_70(self):
        changes = [_make_change("db-service", "database_migration")]
        forecast = generate_forecast(changes, {}, [])
        assert len(forecast.forecasts) == 1
        f = forecast.forecasts[0]
        # Base risk 70 → SEVERE
        assert f.risk_score >= 70.0
        assert f.risk_level in (RiskLevel.HIGH, RiskLevel.SEVERE)

    def test_deployment_base_risk_40(self):
        changes = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        f = forecast.forecasts[0]
        assert f.risk_score >= 40.0

    def test_config_change_base_risk_30(self):
        changes = [_make_change("api", "config_change")]
        forecast = generate_forecast(changes, {}, [])
        f = forecast.forecasts[0]
        assert f.risk_score >= 30.0

    def test_maintenance_base_risk_20(self):
        changes = [_make_change("api", "maintenance")]
        forecast = generate_forecast(changes, {}, [])
        f = forecast.forecasts[0]
        assert f.risk_score >= 20.0

    def test_deployment_lower_than_database_migration(self):
        dep_changes = [_make_change("api", "deployment")]
        db_changes  = [_make_change("api", "database_migration")]
        dep_forecast = generate_forecast(dep_changes, {}, [])
        db_forecast  = generate_forecast(db_changes, {}, [])
        assert dep_forecast.forecasts[0].risk_score < db_forecast.forecasts[0].risk_score

    def test_maintenance_lowest_base_risk(self):
        maint = generate_forecast([_make_change("api", "maintenance")], {}, [])
        dep   = generate_forecast([_make_change("api", "deployment")], {}, [])
        assert maint.forecasts[0].risk_score < dep.forecasts[0].risk_score


# ---------------------------------------------------------------------------
# Historical pattern boost
# ---------------------------------------------------------------------------

class TestHistoricalBoost:

    def test_historical_boost_applied_when_recent_incident(self):
        changes     = [_make_change("payment-api", "deployment")]
        experiences = [_make_experience("payment-api", "error_spike", days_ago=3)]
        forecast    = generate_forecast(changes, {}, experiences)
        f = forecast.forecasts[0]
        # Base 40 + historical 25 = 65
        assert f.risk_score >= 65.0

    def test_no_historical_boost_when_no_matching_service(self):
        changes     = [_make_change("payment-api", "deployment")]
        experiences = [_make_experience("other-service", "error_spike", days_ago=3)]
        forecast_with    = generate_forecast(changes, {}, experiences)
        forecast_without = generate_forecast(changes, {}, [])
        # No boost because service doesn't match
        assert abs(forecast_with.forecasts[0].risk_score -
                   forecast_without.forecasts[0].risk_score) < 5.0

    def test_historical_boost_adds_risk_factor(self):
        changes     = [_make_change("payment-api", "deployment")]
        experiences = [_make_experience("payment-api", "error_spike", days_ago=3)]
        forecast    = generate_forecast(changes, {}, experiences)
        factor_types = [rf.factor_type for rf in forecast.forecasts[0].risk_factors]
        assert "historical_pattern" in factor_types

    def test_no_historical_boost_for_old_incidents(self):
        # Incident more than 7 days ago should not trigger boost
        changes     = [_make_change("payment-api", "deployment")]
        experiences = [_make_experience("payment-api", "error_spike", days_ago=10)]
        forecast_old     = generate_forecast(changes, {}, experiences)
        forecast_no_hist = generate_forecast(changes, {}, [])
        assert abs(forecast_old.forecasts[0].risk_score -
                   forecast_no_hist.forecasts[0].risk_score) < 5.0

    def test_had_recent_incident_helper_returns_true_for_match(self):
        experiences = [_make_experience("payment-api", "error_spike", days_ago=3)]
        matched, evidence = _had_recent_incident("payment-api", "deployment", experiences)
        assert matched is True
        assert len(evidence) > 0

    def test_had_recent_incident_helper_returns_false_for_no_match(self):
        matched, evidence = _had_recent_incident("payment-api", "deployment", [])
        assert matched is False
        assert evidence == ""


# ---------------------------------------------------------------------------
# Current health boosts
# ---------------------------------------------------------------------------

class TestCurrentHealthBoosts:

    def test_high_error_rate_adds_boost(self):
        changes  = [_make_change("api", "deployment")]
        health   = {"api": _make_health(error_rate=0.05)}  # 5%
        forecast = generate_forecast(changes, health, [])
        # Base 40 + error_rate 20 = 60
        assert forecast.forecasts[0].risk_score >= 60.0

    def test_normal_error_rate_no_boost(self):
        changes       = [_make_change("api", "deployment")]
        health_high   = {"api": _make_health(error_rate=0.05)}
        health_normal = {"api": _make_health(error_rate=0.005)}
        f_high   = generate_forecast(changes, health_high,   []).forecasts[0]
        f_normal = generate_forecast(changes, health_normal, []).forecasts[0]
        assert f_high.risk_score > f_normal.risk_score

    def test_high_latency_adds_boost(self):
        changes  = [_make_change("api", "deployment")]
        health   = {"api": _make_health(latency_p95=500.0)}  # > 2× baseline (200ms)
        forecast = generate_forecast(changes, health, [])
        # Base 40 + latency 15 = 55
        assert forecast.forecasts[0].risk_score >= 55.0

    def test_low_latency_no_boost(self):
        changes    = [_make_change("api", "deployment")]
        h_high_lat = {"api": _make_health(latency_p95=500.0)}
        h_low_lat  = {"api": _make_health(latency_p95=150.0)}  # below 2× baseline
        f_high = generate_forecast(changes, h_high_lat, []).forecasts[0]
        f_low  = generate_forecast(changes, h_low_lat,  []).forecasts[0]
        assert f_high.risk_score > f_low.risk_score

    def test_high_cpu_adds_boost(self):
        changes  = [_make_change("api", "deployment")]
        health   = {"api": _make_health(cpu=90.0)}  # > 80%
        forecast = generate_forecast(changes, health, [])
        # Base 40 + cpu 10 = 50
        assert forecast.forecasts[0].risk_score >= 50.0

    def test_normal_cpu_no_boost(self):
        changes      = [_make_change("api", "deployment")]
        h_high_cpu   = {"api": _make_health(cpu=90.0)}
        h_normal_cpu = {"api": _make_health(cpu=60.0)}
        f_high   = generate_forecast(changes, h_high_cpu,   []).forecasts[0]
        f_normal = generate_forecast(changes, h_normal_cpu, []).forecasts[0]
        assert f_high.risk_score > f_normal.risk_score

    def test_all_health_boosts_stack(self):
        changes  = [_make_change("api", "deployment")]
        health   = {"api": _make_health(error_rate=0.05, latency_p95=500.0, cpu=90.0)}
        forecast = generate_forecast(changes, health, [])
        # Base 40 + error 20 + latency 15 + cpu 10 = 85
        assert forecast.forecasts[0].risk_score >= 85.0

    def test_health_boost_adds_current_health_risk_factor(self):
        changes  = [_make_change("api", "deployment")]
        health   = {"api": _make_health(error_rate=0.05)}
        forecast = generate_forecast(changes, health, [])
        factor_types = [rf.factor_type for rf in forecast.forecasts[0].risk_factors]
        assert "current_health" in factor_types


# ---------------------------------------------------------------------------
# Risk score capping
# ---------------------------------------------------------------------------

class TestRiskScoreCap:

    def test_risk_score_capped_at_100(self):
        # database_migration (70) + historical (25) + all health boosts (20+15+10) = 140 → cap 100
        changes     = [_make_change("api", "database_migration")]
        experiences = [_make_experience("api", "latency", days_ago=3)]
        health      = {"api": _make_health(error_rate=0.05, latency_p95=500.0, cpu=90.0)}
        forecast    = generate_forecast(changes, health, experiences)
        assert forecast.forecasts[0].risk_score <= 100.0

    def test_risk_score_non_negative(self):
        changes  = [_make_change("api", "maintenance")]
        forecast = generate_forecast(changes, {}, [])
        assert forecast.forecasts[0].risk_score >= 0.0


# ---------------------------------------------------------------------------
# RiskLevel mapping
# ---------------------------------------------------------------------------

class TestRiskLevelMapping:

    def test_low_risk_level(self):
        # maintenance = 20 → MODERATE (just over LOW boundary)
        # Let's use a very low base — we need to produce a LOW result
        # maintenance = 20 → maps to MODERATE; there's no lower change type
        # so we test with score=10 directly
        assert _score_to_risk_level(10.0) == RiskLevel.LOW

    def test_moderate_risk_level(self):
        assert _score_to_risk_level(35.0) == RiskLevel.MODERATE

    def test_high_risk_level(self):
        assert _score_to_risk_level(60.0) == RiskLevel.HIGH

    def test_severe_risk_level(self):
        assert _score_to_risk_level(80.0) == RiskLevel.SEVERE

    def test_deployment_maps_to_moderate_without_boosts(self):
        changes  = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        f = forecast.forecasts[0]
        assert f.risk_level == RiskLevel.MODERATE

    def test_database_migration_maps_to_high_or_severe(self):
        changes  = [_make_change("api", "database_migration")]
        forecast = generate_forecast(changes, {}, [])
        f = forecast.forecasts[0]
        assert f.risk_level in (RiskLevel.HIGH, RiskLevel.SEVERE)


# ---------------------------------------------------------------------------
# WeatherForecast.get_highest_risk_services
# ---------------------------------------------------------------------------

class TestGetHighestRiskServices:

    def _make_multi_service_forecast(self) -> WeatherForecast:
        changes = [
            _make_change("api",      "deployment"),
            _make_change("db",       "database_migration"),
            _make_change("cache",    "config_change"),
            _make_change("frontend", "maintenance"),
        ]
        health = {
            "api": _make_health(error_rate=0.05),
            "db":  _make_health(cpu=90.0),
        }
        experiences = [_make_experience("db", "latency", days_ago=2)]
        return generate_forecast(changes, health, experiences)

    def test_returns_correct_number(self):
        wf = self._make_multi_service_forecast()
        top3 = wf.get_highest_risk_services(3)
        assert len(top3) == 3

    def test_returns_highest_risk_first(self):
        wf   = self._make_multi_service_forecast()
        top3 = wf.get_highest_risk_services(3)
        for i in range(len(top3) - 1):
            assert top3[i].risk_score >= top3[i + 1].risk_score

    def test_n_larger_than_forecasts_returns_all(self):
        changes  = [_make_change("only-service", "deployment")]
        wf       = generate_forecast(changes, {}, [])
        result   = wf.get_highest_risk_services(10)
        assert len(result) == len(wf.forecasts)

    def test_default_n_is_3(self):
        changes = [
            _make_change("a", "deployment"),
            _make_change("b", "database_migration"),
            _make_change("c", "config_change"),
            _make_change("d", "maintenance"),
        ]
        wf     = generate_forecast(changes, {}, [])
        result = wf.get_highest_risk_services()
        assert len(result) == 3

    def test_returns_sorted_by_risk_score_descending(self):
        changes = [
            _make_change("low",  "maintenance"),
            _make_change("high", "database_migration"),
            _make_change("mid",  "deployment"),
        ]
        wf     = generate_forecast(changes, {}, [])
        top    = wf.get_highest_risk_services(3)
        scores = [f.risk_score for f in top]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Overall system risk
# ---------------------------------------------------------------------------

class TestOverallSystemRisk:

    def test_overall_risk_matches_highest_service(self):
        changes = [
            _make_change("api", "database_migration"),  # SEVERE
            _make_change("ui",  "maintenance"),         # LOW/MODERATE
        ]
        wf = generate_forecast(changes, {}, [])
        assert wf.overall_system_risk == wf.get_highest_risk_services(1)[0].risk_level

    def test_empty_forecasts_gives_low_overall_risk(self):
        wf = generate_forecast([], {}, [])
        assert wf.overall_system_risk == RiskLevel.LOW

    def test_overall_risk_level_helper(self):
        forecasts = [
            ServiceForecast(
                service="a", risk_level=RiskLevel.HIGH, risk_score=60.0,
                predicted_incident_types=[], risk_window_start="", risk_window_end="",
                risk_factors=[], recommended_preemptive_actions=[], confidence=0.7,
            ),
            ServiceForecast(
                service="b", risk_level=RiskLevel.SEVERE, risk_score=85.0,
                predicted_incident_types=[], risk_window_start="", risk_window_end="",
                risk_factors=[], recommended_preemptive_actions=[], confidence=0.8,
            ),
        ]
        assert _overall_risk_level(forecasts) == RiskLevel.SEVERE


# ---------------------------------------------------------------------------
# Headline generation
# ---------------------------------------------------------------------------

class TestHeadlineGeneration:

    def test_headline_not_empty(self):
        changes  = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        assert isinstance(forecast.headline, str) and len(forecast.headline) > 0

    def test_headline_mentions_highest_risk_service(self):
        changes = [
            _make_change("payment-api", "database_migration"),
            _make_change("frontend",    "maintenance"),
        ]
        forecast = generate_forecast(changes, {}, [])
        assert "payment-api" in forecast.headline

    def test_empty_changes_produces_fallback_headline(self):
        wf = generate_forecast([], {}, [])
        assert "No significant" in wf.headline or len(wf.headline) > 0

    def test_headline_mentions_db_migration_count(self):
        changes = [
            _make_change("db1", "database_migration"),
            _make_change("db2", "database_migration"),
        ]
        forecast = generate_forecast(changes, {}, [])
        assert "migration" in forecast.headline.lower() or "DB" in forecast.headline


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

class TestConfidence:

    def test_confidence_higher_with_historical_data(self):
        changes     = [_make_change("api", "deployment")]
        experiences = [_make_experience("api", "error_spike", days_ago=3)]
        f_with_hist    = generate_forecast(changes, {}, experiences).forecasts[0]
        f_without_hist = generate_forecast(changes, {}, []).forecasts[0]
        assert f_with_hist.confidence >= f_without_hist.confidence

    def test_confidence_higher_with_health_data(self):
        changes    = [_make_change("api", "deployment")]
        health     = {"api": _make_health()}
        f_with    = generate_forecast(changes, health, []).forecasts[0]
        f_without = generate_forecast(changes, {},     []).forecasts[0]
        assert f_with.confidence >= f_without.confidence

    def test_confidence_between_zero_and_one(self):
        changes     = [_make_change("api", "database_migration")]
        experiences = [_make_experience("api", "latency", days_ago=3)]
        health      = {"api": _make_health(error_rate=0.05)}
        forecast    = generate_forecast(changes, health, experiences)
        for f in forecast.forecasts:
            assert 0.0 <= f.confidence <= 1.0


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------

class TestEmptyInputs:

    def test_empty_changes_returns_empty_forecasts(self):
        wf = generate_forecast([], {}, [])
        assert wf.forecasts == []

    def test_empty_historical_experiences_no_error(self):
        changes  = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        assert len(forecast.forecasts) == 1

    def test_empty_health_no_error(self):
        changes  = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        assert len(forecast.forecasts) == 1

    def test_all_empty_returns_valid_forecast(self):
        wf = generate_forecast([], {}, [])
        assert isinstance(wf, WeatherForecast)
        assert wf.overall_system_risk == RiskLevel.LOW


# ---------------------------------------------------------------------------
# Predicted incident types
# ---------------------------------------------------------------------------

class TestPredictedIncidentTypes:

    def test_database_migration_predicts_latency_and_timeout(self):
        changes  = [_make_change("db", "database_migration")]
        forecast = generate_forecast(changes, {}, [])
        types    = forecast.forecasts[0].predicted_incident_types
        assert "latency" in types
        assert "timeout" in types

    def test_deployment_predicts_error_spike(self):
        changes  = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        types    = forecast.forecasts[0].predicted_incident_types
        assert "error_spike" in types

    def test_high_error_rate_adds_error_spike_to_types(self):
        changes  = [_make_change("api", "maintenance")]
        health   = {"api": _make_health(error_rate=0.05)}
        forecast = generate_forecast(changes, health, [])
        types    = forecast.forecasts[0].predicted_incident_types
        assert "error_spike" in types

    def test_high_cpu_adds_saturation_to_types(self):
        changes  = [_make_change("api", "maintenance")]
        health   = {"api": _make_health(cpu=90.0)}
        forecast = generate_forecast(changes, health, [])
        types    = forecast.forecasts[0].predicted_incident_types
        assert "saturation" in types


# ---------------------------------------------------------------------------
# Preemptive actions
# ---------------------------------------------------------------------------

class TestPreemptiveActions:

    def test_preemptive_actions_not_empty(self):
        changes  = [_make_change("api", "deployment")]
        forecast = generate_forecast(changes, {}, [])
        assert len(forecast.forecasts[0].recommended_preemptive_actions) > 0

    def test_database_migration_includes_rollback_action(self):
        changes  = [_make_change("db", "database_migration")]
        forecast = generate_forecast(changes, {}, [])
        actions  = " ".join(forecast.forecasts[0].recommended_preemptive_actions).lower()
        assert "rollback" in actions


# ---------------------------------------------------------------------------
# WeatherForecast metadata
# ---------------------------------------------------------------------------

class TestForecastMetadata:

    def test_generated_at_is_iso8601(self):
        wf = generate_forecast([], {}, [])
        # Should parse without error
        parsed = datetime.fromisoformat(wf.generated_at.replace("Z", "+00:00"))
        assert parsed is not None

    def test_forecast_horizon_hours_set(self):
        wf = generate_forecast([], {}, [], forecast_hours=48)
        assert wf.forecast_horizon_hours == 48

    def test_forecasts_sorted_highest_risk_first(self):
        changes = [
            _make_change("maint", "maintenance"),
            _make_change("db",    "database_migration"),
            _make_change("api",   "deployment"),
        ]
        wf = generate_forecast(changes, {}, [])
        scores = [f.risk_score for f in wf.forecasts]
        assert scores == sorted(scores, reverse=True)
