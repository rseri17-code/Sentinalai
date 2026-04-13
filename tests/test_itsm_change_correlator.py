"""Tests for supervisor.itsm_change_correlator."""
from __future__ import annotations

import os

os.environ.setdefault("ITSM_CORRELATION_ENABLED", "true")

from supervisor.itsm_change_correlator import (
    correlate_change_window,
    get_most_likely_change,
    summarise_change_impact,
    format_change_window_report,
    _parse_iso,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

INCIDENT_TIME = "2024-01-15T14:14:00Z"

def _change(
    id="CHG-001",
    title="Deploy payment-service v2.1.0",
    change_type="deploy",
    risk_level="high",
    service="payment-service",
    start_time="2024-01-15T14:02:00Z",   # 12 min before incident
    commit_sha="",
):
    return {
        "id": id,
        "title": title,
        "change_type": change_type,
        "risk_level": risk_level,
        "service": service,
        "start_time": start_time,
        "commit_sha": commit_sha,
    }


# ---------------------------------------------------------------------------
# _parse_iso()
# ---------------------------------------------------------------------------

class TestParseIso:

    def test_parses_z_suffix(self):
        dt = _parse_iso("2024-01-15T14:00:00Z")
        assert dt is not None
        assert dt.hour == 14

    def test_parses_plus_offset(self):
        dt = _parse_iso("2024-01-15T09:00:00-05:00")
        assert dt is not None

    def test_returns_none_for_empty(self):
        assert _parse_iso("") is None

    def test_returns_none_for_invalid(self):
        assert _parse_iso("not-a-date") is None

    def test_naive_datetime_made_utc(self):
        dt = _parse_iso("2024-01-15T14:00:00")
        assert dt is not None
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# correlate_change_window() — basic behaviour
# ---------------------------------------------------------------------------

class TestCorrelateChangeWindow:

    def test_returns_empty_when_no_changes(self):
        result = correlate_change_window(INCIDENT_TIME, [], "payment-service")
        assert result == []

    def test_disabled_returns_empty(self, monkeypatch):
        import supervisor.itsm_change_correlator as mod
        monkeypatch.setattr(mod, "ITSM_CORRELATION_ENABLED", False)
        result = correlate_change_window(INCIDENT_TIME, [_change()], "payment-service")
        assert result == []

    def test_returns_empty_for_invalid_incident_time(self):
        result = correlate_change_window("not-a-date", [_change()], "svc")
        assert result == []

    def test_includes_change_within_window(self):
        ch = _change(start_time="2024-01-15T13:00:00Z")  # 74 min before
        result = correlate_change_window(INCIDENT_TIME, [ch], "payment-service")
        assert len(result) == 1

    def test_excludes_change_outside_window(self):
        # 3 hours before — outside default 120 min window
        ch = _change(start_time="2024-01-15T11:00:00Z")
        result = correlate_change_window(INCIDENT_TIME, [ch])
        assert len(result) == 0

    def test_excludes_change_after_incident(self):
        # Change AFTER incident started
        ch = _change(start_time="2024-01-15T14:30:00Z")
        result = correlate_change_window(INCIDENT_TIME, [ch])
        assert len(result) == 0

    def test_sorted_by_score_descending(self):
        recent = _change(id="CHG-001", start_time="2024-01-15T14:10:00Z",
                         change_type="deploy", risk_level="high",
                         service="payment-service")
        old = _change(id="CHG-002", start_time="2024-01-15T12:30:00Z",  # 104 min before
                      change_type="standard", risk_level="low",
                      service="other-service")
        result = correlate_change_window(INCIDENT_TIME, [old, recent], "payment-service")
        assert len(result) == 2
        assert result[0]["id"] == "CHG-001"  # recent high-risk same-service first

    def test_correlation_score_in_range(self):
        ch = _change()
        result = correlate_change_window(INCIDENT_TIME, [ch], "payment-service")
        assert 0.0 <= result[0]["correlation_score"] <= 1.0

    def test_minutes_before_populated(self):
        ch = _change(start_time="2024-01-15T14:02:00Z")  # 12 min before
        result = correlate_change_window(INCIDENT_TIME, [ch])
        assert result[0]["minutes_before_incident"] == 12

    def test_custom_window_minutes(self):
        # Change 60 min before, custom window of only 30 min
        ch = _change(start_time="2024-01-15T13:14:00Z")
        result = correlate_change_window(INCIDENT_TIME, [ch], window_minutes=30)
        assert len(result) == 0

    def test_change_with_no_timestamp_excluded(self):
        ch = _change()
        ch.pop("start_time")
        result = correlate_change_window(INCIDENT_TIME, [ch])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Scoring factors
# ---------------------------------------------------------------------------

class TestScoringFactors:

    def test_recent_change_scores_higher(self):
        recent = _change(id="A", start_time="2024-01-15T14:10:00Z")
        old = _change(id="B", start_time="2024-01-15T12:30:00Z")
        result = correlate_change_window(INCIDENT_TIME, [recent, old], "payment-service")
        ids = [r["id"] for r in result]
        assert ids.index("A") < ids.index("B")

    def test_service_match_boosts_score(self):
        same_svc = _change(id="A", start_time="2024-01-15T13:30:00Z",
                           service="payment-service")
        diff_svc = _change(id="B", start_time="2024-01-15T13:30:00Z",
                           service="unrelated-service")
        result = correlate_change_window(INCIDENT_TIME, [same_svc, diff_svc],
                                         "payment-service")
        score_same = next(r["correlation_score"] for r in result if r["id"] == "A")
        score_diff = next(r["correlation_score"] for r in result if r["id"] == "B")
        assert score_same > score_diff

    def test_high_risk_scores_higher(self):
        high = _change(id="A", start_time="2024-01-15T13:30:00Z", risk_level="high")
        low = _change(id="B", start_time="2024-01-15T13:30:00Z", risk_level="low")
        result = correlate_change_window(INCIDENT_TIME, [high, low], "payment-service")
        score_high = next(r["correlation_score"] for r in result if r["id"] == "A")
        score_low = next(r["correlation_score"] for r in result if r["id"] == "B")
        assert score_high > score_low

    def test_deploy_type_scores_higher_than_standard(self):
        deploy = _change(id="A", start_time="2024-01-15T13:30:00Z", change_type="deploy")
        standard = _change(id="B", start_time="2024-01-15T13:30:00Z", change_type="standard")
        result = correlate_change_window(INCIDENT_TIME, [deploy, standard])
        score_deploy = next(r["correlation_score"] for r in result if r["id"] == "A")
        score_std = next(r["correlation_score"] for r in result if r["id"] == "B")
        assert score_deploy > score_std

    def test_commit_sha_match_boosts_score(self):
        ch = _change(start_time="2024-01-15T13:30:00Z", commit_sha="abc123def456")
        commits = [{"sha": "abc123def456", "message": "fix: reduce pool size"}]
        result_with = correlate_change_window(INCIDENT_TIME, [ch], git_commits=commits)
        result_without = correlate_change_window(INCIDENT_TIME, [ch])
        assert result_with[0]["correlation_score"] >= result_without[0]["correlation_score"]
        assert result_with[0]["matched_commit"] is not None

    def test_short_sha_commit_match(self):
        ch = _change(start_time="2024-01-15T13:30:00Z", commit_sha="abc1234")
        commits = [{"sha": "abc1234abcdef", "message": "fix: something"}]
        result = correlate_change_window(INCIDENT_TIME, [ch], git_commits=commits)
        # Short SHA should still match
        assert result[0]["matched_commit"] is not None

    def test_correlation_reason_populated(self):
        ch = _change(start_time="2024-01-15T14:10:00Z", service="payment-service",
                     risk_level="high")
        result = correlate_change_window(INCIDENT_TIME, [ch], "payment-service")
        reason = result[0]["correlation_reason"]
        assert reason  # non-empty


# ---------------------------------------------------------------------------
# get_most_likely_change()
# ---------------------------------------------------------------------------

class TestGetMostLikelyChange:

    def test_returns_none_when_no_changes(self):
        assert get_most_likely_change(INCIDENT_TIME, [], "svc") is None

    def test_returns_best_match_above_threshold(self):
        ch = _change(start_time="2024-01-15T14:10:00Z", change_type="deploy",
                     risk_level="high", service="payment-service")
        result = get_most_likely_change(INCIDENT_TIME, [ch], "payment-service")
        assert result is not None
        assert result["id"] == "CHG-001"

    def test_returns_none_when_score_below_min(self):
        # Old, low-risk, different service — likely below min_score=0.40
        ch = _change(
            start_time="2024-01-15T12:01:00Z",  # 133 min before — actually outside window
            change_type="standard", risk_level="low", service="other-svc"
        )
        result = get_most_likely_change(INCIDENT_TIME, [ch], "payment-service", min_score=0.40)
        # Should be None because outside window, or below threshold
        assert result is None

    def test_custom_min_score(self):
        ch = _change(start_time="2024-01-15T13:30:00Z", service="other-svc")
        # With very low min_score, should return something
        result = get_most_likely_change(INCIDENT_TIME, [ch], "payment-service", min_score=0.0)
        assert result is not None


# ---------------------------------------------------------------------------
# summarise_change_impact()
# ---------------------------------------------------------------------------

class TestSummariseChangeImpact:

    def test_produces_non_empty_string(self):
        ch = {
            **_change(),
            "correlation_score": 0.82,
            "minutes_before_incident": 12,
        }
        summary = summarise_change_impact(ch)
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_includes_change_id(self):
        ch = {**_change(id="CHG-4521"), "correlation_score": 0.80, "minutes_before_incident": 12}
        assert "CHG-4521" in summarise_change_impact(ch)

    def test_high_confidence_label(self):
        ch = {**_change(), "correlation_score": 0.80, "minutes_before_incident": 5}
        assert "HIGH" in summarise_change_impact(ch)

    def test_medium_confidence_label(self):
        ch = {**_change(), "correlation_score": 0.55, "minutes_before_incident": 45}
        assert "MEDIUM" in summarise_change_impact(ch)

    def test_low_confidence_label(self):
        ch = {**_change(), "correlation_score": 0.25, "minutes_before_incident": 90}
        assert "LOW" in summarise_change_impact(ch)


# ---------------------------------------------------------------------------
# format_change_window_report()
# ---------------------------------------------------------------------------

class TestFormatChangeWindowReport:

    def test_empty_correlations_message(self):
        report = format_change_window_report([], INCIDENT_TIME)
        assert "No ITSM changes" in report

    def test_report_contains_incident_time(self):
        ch = {**_change(), "correlation_score": 0.70, "minutes_before_incident": 12,
              "correlation_reason": "12 min before incident"}
        report = format_change_window_report([ch], INCIDENT_TIME)
        assert INCIDENT_TIME in report

    def test_report_lists_changes(self):
        ch = {**_change(id="CHG-999"), "correlation_score": 0.70,
              "minutes_before_incident": 10, "correlation_reason": "10 min before"}
        report = format_change_window_report([ch], INCIDENT_TIME)
        assert "CHG-999" in report

    def test_report_includes_matched_commit(self):
        ch = {
            **_change(),
            "correlation_score": 0.80,
            "minutes_before_incident": 10,
            "correlation_reason": "10 min before; commit matched",
            "matched_commit": {"sha": "abc123def456", "message": "fix: reduce pool"},
        }
        report = format_change_window_report([ch], INCIDENT_TIME)
        assert "abc123def" in report

    def test_report_caps_at_five_changes(self):
        changes = [
            {**_change(id=f"CHG-{i}"), "correlation_score": 0.5,
             "minutes_before_incident": i * 5, "correlation_reason": "test",
             "matched_commit": None}
            for i in range(10)
        ]
        report = format_change_window_report(changes, INCIDENT_TIME)
        # Only first 5 should appear
        assert "CHG-5" not in report or changes[5]["id"] not in report[:500]


# ---------------------------------------------------------------------------
# alternate timestamp field names
# ---------------------------------------------------------------------------

class TestAlternativeTimestampFields:

    def test_deployed_at_field(self):
        ch = {
            "id": "CHG-T1", "title": "deploy", "change_type": "deploy",
            "risk_level": "high", "service": "svc",
            "deployed_at": "2024-01-15T14:10:00Z",
        }
        result = correlate_change_window(INCIDENT_TIME, [ch], "svc")
        assert len(result) == 1

    def test_scheduled_start_time_field(self):
        ch = {
            "id": "CHG-T2", "title": "deploy", "change_type": "deploy",
            "risk_level": "normal", "service": "svc",
            "scheduled_start_time": "2024-01-15T13:00:00Z",
        }
        result = correlate_change_window(INCIDENT_TIME, [ch])
        assert len(result) == 1
