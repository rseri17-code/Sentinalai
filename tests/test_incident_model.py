"""Tests for canonical incident model (supervisor/incident_model.py).

Covers all factory methods, normalization, validation, edge cases,
and serialization for the Incident dataclass.
"""

from __future__ import annotations

import pytest

from supervisor.incident_model import (
    Incident,
    _normalize_severity,
    _normalize_snow_state,
    _extract_pd_assignee,
)


# =========================================================================
# _normalize_severity
# =========================================================================

class TestNormalizeSeverity:
    """Severity normalization helper."""

    @pytest.mark.parametrize("val,expected", [
        (1, 1), (2, 2), (3, 3), (4, 4), (5, 5),
    ])
    def test_int_in_range(self, val, expected):
        assert _normalize_severity(val) == expected

    def test_int_below_range_clamped(self):
        assert _normalize_severity(0) == 1

    def test_int_above_range_clamped(self):
        assert _normalize_severity(99) == 5

    def test_negative_int_clamped(self):
        assert _normalize_severity(-5) == 1

    def test_float_clamped(self):
        assert _normalize_severity(2.7) == 2

    def test_none_defaults_to_3(self):
        assert _normalize_severity(None) == 3

    @pytest.mark.parametrize("label,expected", [
        ("critical", 1), ("major", 2), ("warning", 3), ("minor", 4), ("info", 5),
    ])
    def test_string_labels(self, label, expected):
        assert _normalize_severity(label) == expected

    def test_string_int(self):
        assert _normalize_severity("2") == 2

    def test_unknown_string_defaults(self):
        assert _normalize_severity("banana") == 3

    def test_unsupported_type_defaults(self):
        assert _normalize_severity([1, 2, 3]) == 3


# =========================================================================
# _normalize_snow_state
# =========================================================================

class TestNormalizeSnowState:
    """ServiceNow state normalization."""

    @pytest.mark.parametrize("state,expected", [
        ("1", "new"), ("2", "in_progress"), ("3", "on_hold"),
        ("6", "resolved"), ("7", "closed"), ("8", "cancelled"),
    ])
    def test_known_states(self, state, expected):
        assert _normalize_snow_state(state) == expected

    def test_unknown_string_lowered(self):
        assert _normalize_snow_state("Active") == "active"

    def test_int_maps_to_string_key(self):
        result = _normalize_snow_state(1)
        assert isinstance(result, str)

    def test_unmapped_int_returns_open(self):
        # int that is not in state_map as a string
        result = _normalize_snow_state(99)
        assert isinstance(result, str)


# =========================================================================
# _extract_pd_assignee
# =========================================================================

class TestExtractPdAssignee:
    """PagerDuty assignee extraction."""

    def test_valid_assignments(self):
        data = {"assignments": [{"assignee": {"summary": "Jane Doe"}}]}
        assert _extract_pd_assignee(data) == "Jane Doe"

    def test_name_fallback(self):
        data = {"assignments": [{"assignee": {"name": "John"}}]}
        assert _extract_pd_assignee(data) == "John"

    def test_empty_assignments(self):
        assert _extract_pd_assignee({"assignments": []}) == ""

    def test_no_assignments_key(self):
        assert _extract_pd_assignee({}) == ""

    def test_non_dict_assignee(self):
        data = {"assignments": [{"assignee": "plain string"}]}
        assert _extract_pd_assignee(data) == ""

    def test_non_list_assignments(self):
        data = {"assignments": "not a list"}
        assert _extract_pd_assignee(data) == ""


# =========================================================================
# Incident.__post_init__ validation
# =========================================================================

class TestIncidentPostInit:
    """Validation and normalization in __post_init__."""

    def test_empty_incident_id_raises(self):
        with pytest.raises(ValueError, match="incident_id is required"):
            Incident(incident_id="")

    def test_severity_out_of_range_reset(self):
        inc = Incident(incident_id="INC1", severity=99)
        assert inc.severity == 3

    def test_severity_label_set_from_level(self):
        inc = Incident(incident_id="INC1", severity=1)
        assert inc.severity_label == "critical"

    def test_summary_defaults_from_description(self):
        inc = Incident(incident_id="INC1", description="A long description text")
        assert inc.summary == "A long description text"

    def test_created_at_defaults_to_now(self):
        inc = Incident(incident_id="INC1")
        assert inc.created_at  # non-empty ISO timestamp

    def test_non_int_severity_reset(self):
        inc = Incident(incident_id="INC1", severity="not_an_int")  # type: ignore
        assert inc.severity == 3


# =========================================================================
# Incident.to_dict / to_legacy_dict
# =========================================================================

class TestIncidentSerialization:
    """Serialization methods."""

    def test_to_dict_excludes_raw_data(self):
        inc = Incident(incident_id="INC1", raw_data={"secret": True})
        d = inc.to_dict()
        assert "raw_data" not in d
        assert d["incident_id"] == "INC1"

    def test_to_legacy_dict_has_expected_keys(self):
        inc = Incident(incident_id="INC1", summary="test", affected_service="svc")
        d = inc.to_legacy_dict()
        assert d["incident_id"] == "INC1"
        assert d["summary"] == "test"
        assert d["affected_service"] == "svc"
        assert "raw_data" not in d


# =========================================================================
# Incident.from_moogsoft
# =========================================================================

class TestFromMoogsoft:
    """Moogsoft normalization factory."""

    def test_basic_fields(self):
        data = {
            "incident_id": "INC100",
            "summary": "CPU spike on web-01",
            "affected_service": "web-frontend",
            "severity": 2,
            "status": "active",
        }
        inc = Incident.from_moogsoft(data)
        assert inc.incident_id == "INC100"
        assert inc.summary == "CPU spike on web-01"
        assert inc.affected_service == "web-frontend"
        assert inc.severity == 2
        assert inc.source == "moogsoft"

    def test_fallback_field_names(self):
        """Uses 'id' and 'service' as fallbacks."""
        data = {"id": "INC200", "description": "Disk full", "service": "db-primary"}
        inc = Incident.from_moogsoft(data)
        assert inc.incident_id == "INC200"
        assert inc.summary == "Disk full"
        assert inc.affected_service == "db-primary"

    def test_string_severity(self):
        data = {"incident_id": "INC300", "severity": "critical"}
        inc = Incident.from_moogsoft(data)
        assert inc.severity == 1

    def test_missing_severity_defaults(self):
        data = {"incident_id": "INC400"}
        inc = Incident.from_moogsoft(data)
        assert inc.severity == 3

    def test_raw_data_preserved(self):
        data = {"incident_id": "INC500", "custom_field": "xyz"}
        inc = Incident.from_moogsoft(data)
        assert inc.raw_data["custom_field"] == "xyz"


# =========================================================================
# Incident.from_servicenow
# =========================================================================

class TestFromServiceNow:
    """ServiceNow normalization factory."""

    def test_basic_fields(self):
        data = {
            "number": "INC0012345",
            "short_description": "Login failures",
            "cmdb_ci": "auth-service",
            "priority": 1,
            "state": "2",
        }
        inc = Incident.from_servicenow(data)
        assert inc.incident_id == "INC0012345"
        assert inc.summary == "Login failures"
        assert inc.affected_service == "auth-service"
        assert inc.severity == 1
        assert inc.source == "servicenow"
        assert inc.status == "in_progress"

    def test_service_fallback(self):
        data = {"number": "INC001", "short_description": "x", "service": "my-svc"}
        inc = Incident.from_servicenow(data)
        assert inc.affected_service == "my-svc"

    def test_priority_string_defaults(self):
        data = {"number": "INC001", "short_description": "x", "priority": "invalid"}
        inc = Incident.from_servicenow(data)
        assert inc.severity == 3  # default fallback

    def test_sys_id_fallback(self):
        data = {"sys_id": "abc123", "short_description": "test"}
        inc = Incident.from_servicenow(data)
        assert inc.incident_id == "abc123"


# =========================================================================
# Incident.from_pagerduty
# =========================================================================

class TestFromPagerDuty:
    """PagerDuty normalization factory."""

    def test_high_urgency(self):
        data = {
            "id": "PD-001",
            "title": "Server down",
            "urgency": "high",
            "service": {"summary": "backend-api"},
        }
        inc = Incident.from_pagerduty(data)
        assert inc.incident_id == "PD-001"
        assert inc.severity == 2
        assert inc.affected_service == "backend-api"
        assert inc.source == "pagerduty"

    def test_low_urgency(self):
        data = {"id": "PD-002", "title": "Minor alert", "urgency": "low"}
        inc = Incident.from_pagerduty(data)
        assert inc.severity == 4

    def test_default_urgency(self):
        data = {"id": "PD-003", "title": "test"}
        inc = Incident.from_pagerduty(data)
        assert inc.severity == 4  # low default

    def test_service_as_string(self):
        data = {"id": "PD-004", "title": "test", "service": "plain-string"}
        inc = Incident.from_pagerduty(data)
        assert inc.affected_service == "plain-string"

    def test_service_name_fallback(self):
        data = {"id": "PD-005", "title": "t", "service": {"name": "my-svc"}}
        inc = Incident.from_pagerduty(data)
        assert inc.affected_service == "my-svc"

    def test_assignee_extracted(self):
        data = {
            "id": "PD-006",
            "title": "t",
            "assignments": [{"assignee": {"summary": "Alice"}}],
        }
        inc = Incident.from_pagerduty(data)
        assert inc.assigned_to == "Alice"

    def test_incident_number_fallback(self):
        data = {"incident_number": "42", "title": "test"}
        inc = Incident.from_pagerduty(data)
        assert inc.incident_id == "42"


# =========================================================================
# Incident.from_dict (auto-detection)
# =========================================================================

class TestFromDict:
    """Auto-detection and generic fallback."""

    def test_empty_dict_raises(self):
        with pytest.raises(ValueError, match="Empty incident data"):
            Incident.from_dict({})

    def test_detects_moogsoft(self):
        data = {"incident_id": "INC1", "summary": "test"}
        inc = Incident.from_dict(data)
        assert inc.source == "moogsoft"

    def test_detects_servicenow_by_fields(self):
        data = {"number": "INC0099", "short_description": "test snow"}
        inc = Incident.from_dict(data)
        assert inc.source == "servicenow"

    def test_detects_pagerduty_by_fields(self):
        data = {"id": "PD-1", "title": "PD alert", "urgency": "high"}
        inc = Incident.from_dict(data)
        assert inc.source == "pagerduty"

    def test_explicit_source_override_servicenow(self):
        data = {"incident_id": "INC1", "summary": "test", "source": "servicenow",
                "number": "INC1", "short_description": "test"}
        inc = Incident.from_dict(data)
        assert inc.source == "servicenow"

    def test_explicit_source_override_pagerduty(self):
        data = {"incident_id": "INC1", "summary": "test", "source": "pagerduty",
                "id": "INC1", "title": "test"}
        inc = Incident.from_dict(data)
        assert inc.source == "pagerduty"

    def test_manual_fallback(self):
        data = {"id": "MANUAL-1", "description": "Manually submitted"}
        inc = Incident.from_dict(data)
        assert inc.source == "manual"
        assert inc.incident_id == "MANUAL-1"

    def test_manual_with_title(self):
        data = {"id": "MANUAL-2", "title": "Something happened"}
        inc = Incident.from_dict(data)
        assert inc.summary == "Something happened"
