"""Tests for supervisor/postmortem_generator.py."""

from __future__ import annotations

import pytest

from supervisor.postmortem_generator import (
    ActionItem,
    PostmortemReport,
    generate_postmortem,
    _compute_duration,
    _build_five_whys,
    _build_action_items,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rca(incident_type: str = "timeout",
         severity: str = "High",
         service: str = "payment-service") -> dict:
    return {
        "incident_id": "INC12345",
        "incident_summary": f"{incident_type} on {service}",
        "affected_service": service,
        "incident_type": incident_type,
        "severity_label": severity,
        "root_cause": f"{service} database slow queries",
        "confidence": 92,
        "start_time": "2024-02-12T10:30:00Z",
        "evidence_timeline": [
            {"time": "2024-02-12T10:30:00Z", "event": "Latency spike detected", "source": "sysdig"},
            {"time": "2024-02-12T10:30:15Z", "event": "Timeout alerts fired", "source": "moogsoft"},
        ],
    }


# ---------------------------------------------------------------------------
# generate_postmortem — basic structure
# ---------------------------------------------------------------------------

class TestGeneratePostmortemBasic:
    def test_returns_postmortem_report(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert isinstance(r, PostmortemReport)

    def test_incident_id_preserved(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert r.incident_id == "INC12345"

    def test_status_is_draft(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert r.status == "draft"

    def test_reviewed_by_none(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert r.reviewed_by is None

    def test_severity_preserved(self):
        r = generate_postmortem(_rca(severity="Critical"), "2024-02-12T11:00:00Z")
        assert r.severity == "Critical"

    def test_generated_at_is_string(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert isinstance(r.generated_at, str) and len(r.generated_at) > 0

    def test_executive_summary_contains_service(self):
        r = generate_postmortem(_rca(service="checkout-service"), "2024-02-12T11:00:00Z")
        assert "checkout-service" in r.executive_summary

    def test_executive_summary_contains_root_cause(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "slow queries" in r.executive_summary or "payment-service" in r.executive_summary

    def test_timeline_from_evidence(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert len(r.timeline) >= 2

    def test_team_notes_appended_to_timeline(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z", team_notes=["Manual restart performed"])
        events = [e.get("event", "") for e in r.timeline]
        assert any("Manual restart" in e for e in events)

    def test_similar_incidents_passed_through(self):
        sims = [{"incident_id": "INC99", "root_cause": "same issue", "similarity": 0.9}]
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z", similar_incidents=sims)
        assert len(r.similar_past_incidents) == 1
        assert r.similar_past_incidents[0]["incident_id"] == "INC99"

    def test_empty_similar_incidents_default(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert r.similar_past_incidents == []

    def test_minimal_rca_no_crash(self):
        r = generate_postmortem({}, "2024-02-12T11:00:00Z")
        assert isinstance(r, PostmortemReport)
        assert r.incident_id == "UNKNOWN"


# ---------------------------------------------------------------------------
# Duration computation
# ---------------------------------------------------------------------------

class TestComputeDuration:
    def test_30_minutes(self):
        assert _compute_duration("2024-02-12T10:30:00Z", "2024-02-12T11:00:00Z") == 30

    def test_90_minutes(self):
        assert _compute_duration("2024-02-12T09:00:00Z", "2024-02-12T10:30:00Z") == 90

    def test_invalid_start_returns_zero(self):
        assert _compute_duration("not-a-date", "2024-02-12T11:00:00Z") == 0

    def test_invalid_end_returns_zero(self):
        assert _compute_duration("2024-02-12T10:00:00Z", "not-a-date") == 0

    def test_both_empty_returns_zero(self):
        assert _compute_duration("", "") == 0

    def test_duration_in_report(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:30:00Z")
        assert r.duration_minutes == 60


# ---------------------------------------------------------------------------
# Contributing factors by incident type
# ---------------------------------------------------------------------------

class TestContributingFactors:
    def test_timeout_factors(self):
        r = generate_postmortem(_rca("timeout"), "2024-02-12T11:00:00Z")
        joined = " ".join(r.contributing_factors).lower()
        assert "circuit breaker" in joined or "timeout" in joined

    def test_oomkill_factors(self):
        r = generate_postmortem(_rca("oomkill"), "2024-02-12T11:00:00Z")
        joined = " ".join(r.contributing_factors).lower()
        assert "memory" in joined

    def test_error_spike_factors(self):
        r = generate_postmortem(_rca("error_spike"), "2024-02-12T11:00:00Z")
        joined = " ".join(r.contributing_factors).lower()
        assert "deploy" in joined or "canary" in joined or "test" in joined

    def test_network_factors(self):
        r = generate_postmortem(_rca("network"), "2024-02-12T11:00:00Z")
        joined = " ".join(r.contributing_factors).lower()
        assert "certificate" in joined or "dns" in joined or "network" in joined

    def test_factors_are_systemic_not_personal(self):
        for itype in ["timeout", "oomkill", "error_spike", "latency", "saturation"]:
            r = generate_postmortem(_rca(itype), "2024-02-12T11:00:00Z")
            for factor in r.contributing_factors:
                assert "engineer" not in factor.lower()
                assert "person" not in factor.lower()
                assert "fault of" not in factor.lower()

    def test_at_least_one_factor(self):
        r = generate_postmortem(_rca("cascading"), "2024-02-12T11:00:00Z")
        assert len(r.contributing_factors) >= 1


# ---------------------------------------------------------------------------
# Five Whys
# ---------------------------------------------------------------------------

class TestFiveWhys:
    def test_exactly_five_whys(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert len(r.five_whys) == 5

    def test_five_whys_for_oomkill(self):
        r = generate_postmortem(_rca("oomkill"), "2024-02-12T11:00:00Z")
        assert len(r.five_whys) == 5

    def test_five_whys_for_error_spike(self):
        r = generate_postmortem(_rca("error_spike"), "2024-02-12T11:00:00Z")
        assert len(r.five_whys) == 5

    def test_five_whys_service_interpolated(self):
        r = generate_postmortem(_rca(service="checkout-service"), "2024-02-12T11:00:00Z")
        combined = " ".join(r.five_whys)
        assert "checkout-service" in combined

    def test_five_whys_are_non_empty_strings(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        for why in r.five_whys:
            assert isinstance(why, str) and len(why) > 5


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------

class TestActionItems:
    def test_action_items_non_empty(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert len(r.action_items) >= 1

    def test_action_items_are_action_item_instances(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert all(isinstance(ai, ActionItem) for ai in r.action_items)

    def test_action_item_priority_valid(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        valid_priorities = {"P1", "P2", "P3"}
        for ai in r.action_items:
            assert ai.priority in valid_priorities

    def test_action_item_category_valid(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        valid_cats = {"prevention", "detection", "response", "documentation"}
        for ai in r.action_items:
            assert ai.category in valid_cats

    def test_action_item_due_days_positive(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        for ai in r.action_items:
            assert ai.due_days > 0

    def test_action_item_estimated_effort_valid(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        valid_efforts = {"hours", "days", "weeks"}
        for ai in r.action_items:
            assert ai.estimated_effort in valid_efforts

    def test_action_items_differ_by_type(self):
        r_timeout = generate_postmortem(_rca("timeout"), "2024-02-12T11:00:00Z")
        r_oomkill = generate_postmortem(_rca("oomkill"), "2024-02-12T11:00:00Z")
        titles_timeout = {ai.title for ai in r_timeout.action_items}
        titles_oomkill = {ai.title for ai in r_oomkill.action_items}
        # At least some items should differ between types
        assert titles_timeout != titles_oomkill


# ---------------------------------------------------------------------------
# to_markdown
# ---------------------------------------------------------------------------

class TestToMarkdown:
    def test_contains_executive_summary_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        md = r.to_markdown()
        assert "## Executive Summary" in md

    def test_contains_timeline_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "## Timeline" in r.to_markdown()

    def test_contains_contributing_factors_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "## Contributing Factors" in r.to_markdown()

    def test_contains_what_went_well_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "## What Went Well" in r.to_markdown()

    def test_contains_what_needs_improvement_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "## What Needs Improvement" in r.to_markdown()

    def test_contains_five_whys_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "## 5 Whys" in r.to_markdown()

    def test_contains_action_items_header(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "## Action Items" in r.to_markdown()

    def test_contains_incident_id(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert "INC12345" in r.to_markdown()

    def test_contains_reviewer_when_approved(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        r.approve("sre-lead")
        assert "sre-lead" in r.to_markdown()


# ---------------------------------------------------------------------------
# approve lifecycle
# ---------------------------------------------------------------------------

class TestApproveLifecycle:
    def test_approve_changes_status(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        assert r.status == "draft"
        r.approve("alice")
        assert r.status == "approved"

    def test_approve_sets_reviewer(self):
        r = generate_postmortem(_rca(), "2024-02-12T11:00:00Z")
        r.approve("bob")
        assert r.reviewed_by == "bob"
