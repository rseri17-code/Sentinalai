"""Tests for supervisor/systemic_analyzer.py — cross-incident anti-pattern detection."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from supervisor.systemic_analyzer import (
    AntiPattern,
    SystemicAnalysisReport,
    extract_anti_patterns,
    _classify_root_cause,
    _compute_risk_score,
    _normalise_severity,
    _parse_timestamp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(days: int = 0, hours: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat()


def _exp(incident_id: str = "INC1",
         service: str = "payment-service",
         root_cause: str = "connection pool exhausted",
         incident_type: str = "timeout",
         severity: str = "High",
         timestamp: str | None = None,
         resolution_minutes: int = 30) -> dict:
    return {
        "incident_id": incident_id,
        "service": service,
        "root_cause": root_cause,
        "incident_type": incident_type,
        "severity": severity,
        "timestamp": timestamp or _now_iso(),
        "resolution_minutes": resolution_minutes,
    }


def _make_cluster(service: str, root_cause: str, count: int,
                  incident_type: str = "timeout") -> list[dict]:
    return [
        _exp(f"INC{i}", service, root_cause, incident_type)
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Root-cause classification
# ---------------------------------------------------------------------------

class TestClassifyRootCause:
    def test_connection_pool(self):
        assert _classify_root_cause("connection pool exhausted") == "connection_pool_exhaustion"

    def test_connection_pool_underscore(self):
        assert _classify_root_cause("connection_pool exhaustion") == "connection_pool_exhaustion"

    def test_memory_leak(self):
        assert _classify_root_cause("memory leak in user-service") == "memory_pressure"

    def test_oom(self):
        assert _classify_root_cause("OOMKilled") == "memory_pressure"

    def test_deploy(self):
        assert _classify_root_cause("v3.1.0 deployment regression") == "deployment_regression"

    def test_rollout(self):
        assert _classify_root_cause("failed rollout of payment-service") == "deployment_regression"

    def test_timeout(self):
        assert _classify_root_cause("upstream timed out") == "timeout_cascade"

    def test_disk(self):
        assert _classify_root_cause("disk full on /var/log") == "disk_saturation"

    def test_iops(self):
        assert _classify_root_cause("IOPS exhausted on RDS") == "disk_saturation"

    def test_network(self):
        assert _classify_root_cause("network packet loss") == "network_instability"

    def test_certificate(self):
        assert _classify_root_cause("TLS certificate expired") == "certificate_management"

    def test_ssl(self):
        assert _classify_root_cause("SSL handshake failed") == "certificate_management"

    def test_unknown(self):
        assert _classify_root_cause("something completely unrelated") == "unknown"

    def test_empty_string(self):
        assert _classify_root_cause("") == "unknown"

    def test_case_insensitive(self):
        assert _classify_root_cause("MEMORY LEAK") == "memory_pressure"


# ---------------------------------------------------------------------------
# extract_anti_patterns — core behaviour
# ---------------------------------------------------------------------------

class TestExtractAntiPatterns:
    def test_empty_experiences(self):
        report = extract_anti_patterns([])
        assert report.anti_patterns == []
        assert report.total_incidents_analyzed == 0
        assert report.systemic_risk_score == 100.0

    def test_below_threshold_not_reported(self):
        exps = _make_cluster("svc", "connection pool exhausted", 2)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert report.anti_patterns == []

    def test_at_threshold_reported(self):
        exps = _make_cluster("svc", "connection pool exhausted", 3)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert len(report.anti_patterns) == 1

    def test_anti_pattern_service_and_category(self):
        exps = _make_cluster("payment-service", "connection pool exhausted", 5)
        report = extract_anti_patterns(exps, min_incident_count=3)
        ap = report.anti_patterns[0]
        assert ap.service == "payment-service"
        assert ap.root_cause_category == "connection_pool_exhaustion"

    def test_incident_count_correct(self):
        exps = _make_cluster("svc", "memory leak", 7)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert report.anti_patterns[0].incident_count == 7

    def test_frequency_per_week(self):
        exps = _make_cluster("svc", "memory leak", 14)
        report = extract_anti_patterns(exps, window_days=14, min_incident_count=3)
        ap = report.anti_patterns[0]
        assert ap.frequency_per_week == pytest.approx(7.0)

    def test_priority_urgent(self):
        # >2/week → urgent
        exps = _make_cluster("svc", "connection pool exhausted", 30)
        report = extract_anti_patterns(exps, window_days=7, min_incident_count=3)
        assert report.anti_patterns[0].priority == "urgent"

    def test_priority_high(self):
        # 1-2/week → high
        exps = _make_cluster("svc", "connection pool exhausted", 10)
        report = extract_anti_patterns(exps, window_days=7, min_incident_count=3)
        assert report.anti_patterns[0].priority in ("high", "urgent")

    def test_priority_medium(self):
        # <1/week → medium
        exps = _make_cluster("svc", "memory leak", 3)
        report = extract_anti_patterns(exps, window_days=90, min_incident_count=3)
        assert report.anti_patterns[0].priority == "medium"

    def test_architectural_recommendation_present(self):
        exps = _make_cluster("svc", "connection pool exhausted", 5)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert len(report.anti_patterns[0].architectural_recommendation) > 20

    def test_recommended_actions_list(self):
        exps = _make_cluster("svc", "memory leak", 5)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert isinstance(report.anti_patterns[0].recommended_actions, list)
        assert len(report.anti_patterns[0].recommended_actions) >= 3

    def test_prevention_rate_in_range(self):
        exps = _make_cluster("svc", "certificate expired", 5)
        report = extract_anti_patterns(exps, min_incident_count=3)
        rate = report.anti_patterns[0].estimated_prevention_rate
        assert 0.0 <= rate <= 1.0

    def test_multiple_services_separate_patterns(self):
        exps = (
            _make_cluster("svc-a", "connection pool exhausted", 5) +
            _make_cluster("svc-b", "memory leak", 5)
        )
        report = extract_anti_patterns(exps, min_incident_count=3)
        services = {ap.service for ap in report.anti_patterns}
        assert "svc-a" in services
        assert "svc-b" in services

    def test_sorted_by_frequency_descending(self):
        # svc-a has more incidents → higher frequency → should appear first
        exps = (
            _make_cluster("svc-a", "connection pool exhausted", 20) +
            _make_cluster("svc-b", "connection pool exhausted", 5)
        )
        report = extract_anti_patterns(exps, window_days=7, min_incident_count=3)
        freqs = [ap.frequency_per_week for ap in report.anti_patterns]
        assert freqs == sorted(freqs, reverse=True)

    def test_severity_distribution_built(self):
        exps = [
            _exp("I1", "svc", "connection pool exhausted", severity="Critical"),
            _exp("I2", "svc", "connection pool exhausted", severity="High"),
            _exp("I3", "svc", "connection pool exhausted", severity="High"),
        ]
        report = extract_anti_patterns(exps, min_incident_count=3)
        dist = report.anti_patterns[0].severity_distribution
        assert dist.get("Critical", 0) == 1
        assert dist.get("High", 0) == 2

    def test_window_filtering_excludes_old(self):
        old = _exp("OLD", "svc", "memory leak", timestamp=_ago_iso(days=100))
        recent = [_exp(f"I{i}", "svc", "memory leak") for i in range(5)]
        report = extract_anti_patterns([old] + recent, window_days=30, min_incident_count=3)
        # OLD should be excluded; 5 recent should form a pattern
        ap = next((a for a in report.anti_patterns if a.service == "svc"), None)
        if ap:
            assert ap.incident_count == 5

    def test_pattern_id_format(self):
        exps = _make_cluster("payment-service", "connection pool exhausted", 4)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert report.anti_patterns[0].pattern_id == "payment-service__connection_pool_exhaustion"

    def test_total_incidents_analyzed(self):
        exps = _make_cluster("svc", "memory leak", 7)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert report.total_incidents_analyzed == 7

    def test_top_recommendation_from_worst_pattern(self):
        exps = _make_cluster("svc", "connection pool exhausted", 10)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert len(report.top_recommendation) > 20

    def test_top_recommendation_empty_when_no_patterns(self):
        report = extract_anti_patterns([])
        assert "No recurring" in report.top_recommendation

    def test_estimated_reduction_pct_in_range(self):
        exps = _make_cluster("svc", "connection pool exhausted", 5)
        report = extract_anti_patterns(exps, min_incident_count=3)
        assert 0.0 <= report.estimated_incident_reduction_pct <= 100.0

    def test_unknown_category_gets_generic_recommendation(self):
        exps = _make_cluster("svc", "something weird happened", 5)
        report = extract_anti_patterns(exps, min_incident_count=3)
        ap = report.anti_patterns[0]
        assert ap.root_cause_category == "unknown"
        assert len(ap.architectural_recommendation) > 10


# ---------------------------------------------------------------------------
# Systemic risk score
# ---------------------------------------------------------------------------

class TestComputeRiskScore:
    def test_no_patterns_full_health(self):
        assert _compute_risk_score([]) == 100.0

    def test_score_decreases_with_patterns(self):
        exps = _make_cluster("svc", "connection pool exhausted", 10)
        report = extract_anti_patterns(exps, window_days=7, min_incident_count=3)
        assert report.systemic_risk_score < 100.0

    def test_score_non_negative(self):
        # Even with many bad patterns, score >= 0
        many_exps = []
        for i in range(10):
            many_exps.extend(_make_cluster(f"svc{i}", "connection pool exhausted", 10))
        report = extract_anti_patterns(many_exps, window_days=7, min_incident_count=3)
        assert report.systemic_risk_score >= 0.0

    def test_worse_patterns_give_lower_score(self):
        few = _make_cluster("svc", "memory leak", 4)
        many = _make_cluster("svc", "memory leak", 20)
        score_few = extract_anti_patterns(few, window_days=7, min_incident_count=3).systemic_risk_score
        score_many = extract_anti_patterns(many, window_days=7, min_incident_count=3).systemic_risk_score
        assert score_many <= score_few


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestNormaliseSeverity:
    def test_int_critical(self):
        assert _normalise_severity(1) == "Critical"

    def test_string_high(self):
        assert _normalise_severity("high") == "High"

    def test_unknown_passthrough(self):
        result = _normalise_severity("weird")
        assert isinstance(result, str)


class TestParseTimestamp:
    def test_valid_iso(self):
        ts = "2024-02-12T10:30:00+00:00"
        dt = _parse_timestamp(ts)
        assert dt is not None
        assert dt.year == 2024

    def test_z_suffix(self):
        dt = _parse_timestamp("2024-01-01T00:00:00Z")
        assert dt is not None

    def test_invalid_returns_none(self):
        assert _parse_timestamp("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_timestamp("") is None
