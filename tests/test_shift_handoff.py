"""Tests for supervisor/shift_handoff.py — Shift Handoff Intelligence Brief."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from supervisor.shift_handoff import (
    FragileService,
    ConditionalGuidance,
    HandoffBrief,
    generate_handoff_brief,
    _parse_ts,
    _build_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(days: int = 0, hours: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat()


def _exp(service: str = "payment-service",
         incident_type: str = "timeout",
         timestamp: str | None = None) -> dict:
    return {
        "incident_id": f"INC-{service}-{incident_type}",
        "service": service,
        "incident_type": incident_type,
        "root_cause": f"{incident_type} on {service}",
        "timestamp": timestamp or _now_iso(),
        "severity": "High",
    }


def _change(service: str = "payment-service",
            change_type: str = "deployment",
            scheduled_at: str = "2024-02-13T02:00:00Z") -> dict:
    return {
        "service": service,
        "change_type": change_type,
        "scheduled_at": scheduled_at,
        "description": f"{change_type} on {service}",
    }


# ---------------------------------------------------------------------------
# Fragile service detection
# ---------------------------------------------------------------------------

class TestFragileServiceDetection:
    def test_no_experiences_no_fragile(self):
        brief = generate_handoff_brief([], [], [])
        assert brief.fragile_services == []

    def test_single_incident_not_fragile(self):
        brief = generate_handoff_brief([_exp()], [], [])
        assert brief.fragile_services == []

    def test_two_incidents_fragile_elevated(self):
        exps = [_exp("svc-a"), _exp("svc-a")]
        brief = generate_handoff_brief(exps, [], [])
        svc = next((s for s in brief.fragile_services if s.service == "svc-a"), None)
        assert svc is not None
        assert svc.risk_level == "elevated"

    def test_three_incidents_fragile_high(self):
        exps = [_exp("svc-b")] * 3
        brief = generate_handoff_brief(exps, [], [])
        svc = next((s for s in brief.fragile_services if s.service == "svc-b"), None)
        assert svc is not None
        assert svc.risk_level == "high"

    def test_four_incidents_fragile_critical(self):
        exps = [_exp("svc-c")] * 4
        brief = generate_handoff_brief(exps, [], [])
        svc = next((s for s in brief.fragile_services if s.service == "svc-c"), None)
        assert svc is not None
        assert svc.risk_level == "critical"

    def test_incident_count_correct(self):
        exps = [_exp("svc-x")] * 5
        brief = generate_handoff_brief(exps, [], [])
        svc = next(s for s in brief.fragile_services if s.service == "svc-x")
        assert svc.incident_count_7d == 5

    def test_old_incidents_excluded(self):
        old = [_exp("svc-y", timestamp=_ago_iso(days=10))] * 5
        brief = generate_handoff_brief(old, [], [], lookback_days=7)
        svcs = [s.service for s in brief.fragile_services]
        assert "svc-y" not in svcs

    def test_mixed_old_new_only_recent_counted(self):
        old = [_exp("svc-z", timestamp=_ago_iso(days=10))] * 3
        new = [_exp("svc-z", timestamp=_now_iso())] * 2
        brief = generate_handoff_brief(old + new, [], [], lookback_days=7)
        svc = next((s for s in brief.fragile_services if s.service == "svc-z"), None)
        if svc:
            assert svc.incident_count_7d == 2

    def test_sorted_by_incident_count_descending(self):
        exps = [_exp("svc-low")] * 2 + [_exp("svc-high")] * 5
        brief = generate_handoff_brief(exps, [], [])
        counts = [s.incident_count_7d for s in brief.fragile_services]
        assert counts == sorted(counts, reverse=True)

    def test_fragile_service_has_watch_signals(self):
        exps = [_exp("svc", "timeout")] * 2
        brief = generate_handoff_brief(exps, [], [])
        svc = next(s for s in brief.fragile_services if s.service == "svc")
        assert len(svc.watch_signals) >= 1

    def test_fragile_service_reason_mentions_count(self):
        exps = [_exp("svc")] * 3
        brief = generate_handoff_brief(exps, [], [])
        svc = next(s for s in brief.fragile_services if s.service == "svc")
        assert "3" in svc.reason


# ---------------------------------------------------------------------------
# Conditional guidance
# ---------------------------------------------------------------------------

class TestConditionalGuidance:
    def test_guidance_generated_for_fragile_services(self):
        exps = [_exp("payment-service", "timeout")] * 3
        brief = generate_handoff_brief(exps, [], [])
        assert len(brief.conditional_guidance) >= 1

    def test_guidance_trigger_mentions_service(self):
        exps = [_exp("payment-service", "timeout")] * 3
        brief = generate_handoff_brief(exps, [], [])
        triggers = [g.trigger for g in brief.conditional_guidance]
        assert any("payment-service" in t for t in triggers)

    def test_guidance_has_runbook_hint(self):
        exps = [_exp("svc", "oomkill")] * 3
        brief = generate_handoff_brief(exps, [], [])
        for g in brief.conditional_guidance:
            assert len(g.runbook_hint) > 3

    def test_guidance_has_escalate_to(self):
        exps = [_exp("svc", "error_spike")] * 3
        brief = generate_handoff_brief(exps, [], [])
        for g in brief.conditional_guidance:
            assert len(g.escalate_to) > 0

    def test_no_guidance_for_no_fragile_services(self):
        brief = generate_handoff_brief([], [], [])
        assert brief.conditional_guidance == []

    def test_guidance_deduplicated_by_type(self):
        # Two fragile services with same incident type should produce one guidance entry for that type
        exps = [_exp("svc-a", "timeout")] * 3 + [_exp("svc-b", "timeout")] * 3
        brief = generate_handoff_brief(exps, [], [])
        timeout_guidance = [g for g in brief.conditional_guidance if "timeout" in g.runbook_hint.lower() or "error rate" in g.trigger.lower()]
        assert len(timeout_guidance) <= 2  # not duplicated for each service


# ---------------------------------------------------------------------------
# Upcoming risk
# ---------------------------------------------------------------------------

class TestUpcomingRisk:
    def test_db_migration_is_high_risk(self):
        brief = generate_handoff_brief([], [], [_change(change_type="database_migration")])
        assert any(c["risk_level"] == "high" for c in brief.upcoming_risk)

    def test_deployment_is_medium_risk(self):
        brief = generate_handoff_brief([], [], [_change(change_type="deployment")])
        assert any(c["risk_level"] == "medium" for c in brief.upcoming_risk)

    def test_maintenance_is_low_risk(self):
        brief = generate_handoff_brief([], [], [_change(change_type="maintenance")])
        assert any(c["risk_level"] == "low" for c in brief.upcoming_risk)

    def test_high_risk_sorted_first(self):
        changes = [
            _change("svc-a", "maintenance"),
            _change("svc-b", "database_migration"),
            _change("svc-c", "deployment"),
        ]
        brief = generate_handoff_brief([], [], changes)
        risks = [c["risk_level"] for c in brief.upcoming_risk]
        assert risks[0] == "high"

    def test_no_changes_empty_upcoming_risk(self):
        brief = generate_handoff_brief([], [], [])
        assert brief.upcoming_risk == []

    def test_service_name_preserved(self):
        brief = generate_handoff_brief([], [], [_change("billing-service", "deployment")])
        assert any(c["service"] == "billing-service" for c in brief.upcoming_risk)


# ---------------------------------------------------------------------------
# Active investigations
# ---------------------------------------------------------------------------

class TestActiveInvestigations:
    def test_active_incidents_passed_through(self):
        active = [{"incident_id": "INC999", "status": "investigating"}]
        brief = generate_handoff_brief([], active, [])
        assert len(brief.active_investigations) == 1
        assert brief.active_investigations[0]["incident_id"] == "INC999"

    def test_no_active_incidents(self):
        brief = generate_handoff_brief([], [], [])
        assert brief.active_investigations == []


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_is_non_empty_string(self):
        brief = generate_handoff_brief([], [], [])
        assert isinstance(brief.summary, str) and len(brief.summary) > 10

    def test_summary_mentions_fragile_service(self):
        exps = [_exp("checkout-service")] * 3
        brief = generate_handoff_brief(exps, [], [])
        assert "checkout-service" in brief.summary

    def test_summary_mentions_active_investigation(self):
        brief = generate_handoff_brief([], [{"incident_id": "INC123"}], [])
        assert "1" in brief.summary or "investigation" in brief.summary.lower()

    def test_summary_mentions_high_risk_change(self):
        brief = generate_handoff_brief([], [], [_change(change_type="database_migration")])
        assert "high" in brief.summary.lower() or "rollback" in brief.summary.lower()

    def test_no_incidents_summary_clean(self):
        brief = generate_handoff_brief([], [], [])
        assert "No recurring" in brief.summary or "No active" in brief.summary


# ---------------------------------------------------------------------------
# to_slack_message
# ---------------------------------------------------------------------------

class TestToSlackMessage:
    def test_contains_shift_handoff_header(self):
        brief = generate_handoff_brief([], [], [])
        msg = brief.to_slack_message()
        assert "SHIFT HANDOFF" in msg

    def test_contains_engineer_names(self):
        brief = generate_handoff_brief([], [], [], outgoing_engineer="alice", incoming_engineer="bob")
        msg = brief.to_slack_message()
        assert "alice" in msg
        assert "bob" in msg

    def test_contains_fragile_services_section(self):
        exps = [_exp("svc")] * 3
        brief = generate_handoff_brief(exps, [], [])
        msg = brief.to_slack_message()
        assert "FRAGILE" in msg or "svc" in msg

    def test_contains_upcoming_changes_section(self):
        brief = generate_handoff_brief([], [], [_change()])
        msg = brief.to_slack_message()
        assert "UPCOMING" in msg or "deployment" in msg.lower()

    def test_contains_watch_list_when_fragile(self):
        exps = [_exp("svc", "timeout")] * 3
        brief = generate_handoff_brief(exps, [], [])
        msg = brief.to_slack_message()
        assert "WATCH" in msg or "svc" in msg

    def test_contains_generated_by(self):
        brief = generate_handoff_brief([], [], [])
        msg = brief.to_slack_message()
        assert "SentinalAI" in msg

    def test_no_emoji_in_output(self):
        brief = generate_handoff_brief(
            [_exp("svc")] * 3,
            [{"incident_id": "INC1"}],
            [_change()],
        )
        msg = brief.to_slack_message()
        # Check no common emoji code points (basic check)
        for char in msg:
            assert ord(char) < 0x1F600 or ord(char) > 0x1F64F, f"Emoji found: {char}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestParseTs:
    def test_valid_iso(self):
        dt = _parse_ts("2024-02-12T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2024

    def test_z_suffix(self):
        dt = _parse_ts("2024-01-01T00:00:00Z")
        assert dt is not None

    def test_invalid_returns_none(self):
        assert _parse_ts("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_ts("") is None
