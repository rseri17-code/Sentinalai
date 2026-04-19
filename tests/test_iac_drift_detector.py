"""Tests for supervisor/iac_drift_detector.py

Coverage:
- identical configs (no drift)
- single top-level property drift
- nested property drift
- severity assignment for each path-heuristic bucket
- incident_correlation boost when change was within 24 h
- approve_rollback state transition
- drift_is_likely_root_cause flag
- remediation command/plan generation for each iac_source
- missing key in live config (key present in baseline only)
- approve_rollback raises on already-terminal status
"""
from __future__ import annotations

import pytest

from supervisor.iac_drift_detector import (
    DriftReport,
    DriftSeverity,
    DriftedProperty,
    approve_rollback,
    detect_drift,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_live() -> dict:
    return {
        "replicas": 3,
        "image": "payment-service:v2.3.1",
        "resources": {
            "limits": {"memory": "512Mi", "cpu": "500m"},
            "requests": {"memory": "256Mi", "cpu": "250m"},
        },
        "env": {"LOG_LEVEL": "info", "TIMEOUT_MS": "5000"},
        "annotations": {"owner": "sre-team"},
    }


def _simple_baseline() -> dict:
    return {
        "replicas": 3,
        "image": "payment-service:v2.3.1",
        "resources": {
            "limits": {"memory": "512Mi", "cpu": "500m"},
            "requests": {"memory": "256Mi", "cpu": "250m"},
        },
        "env": {"LOG_LEVEL": "info", "TIMEOUT_MS": "5000"},
        "annotations": {"owner": "sre-team"},
    }


# ---------------------------------------------------------------------------
# No-drift baseline
# ---------------------------------------------------------------------------

class TestNoDrift:
    def test_identical_configs_produce_zero_drifts(self):
        report = detect_drift(
            service="payment-service",
            live_config=_simple_live(),
            iac_baseline=_simple_baseline(),
        )
        assert isinstance(report, DriftReport)
        assert report.total_drift_count == 0
        assert report.drifted_properties == []
        assert report.drift_is_likely_root_cause is False
        assert report.root_cause_confidence == 0.0

    def test_no_drift_report_defaults(self):
        report = detect_drift(
            service="svc",
            live_config={"a": 1},
            iac_baseline={"a": 1},
        )
        assert report.status == "detected"
        assert report.requires_human_approval is True
        assert report.approved_by is None
        assert report.high_severity_count == 0


# ---------------------------------------------------------------------------
# Single property drift
# ---------------------------------------------------------------------------

class TestSinglePropertyDrift:
    def test_detects_changed_top_level_scalar(self):
        live = _simple_live()
        baseline = _simple_baseline()
        baseline["replicas"] = 5          # IaC says 5, live is 3

        report = detect_drift("payment-service", live, baseline)

        assert report.total_drift_count == 1
        dp = report.drifted_properties[0]
        assert dp.property_path == "replicas"
        assert dp.iac_value == 5
        assert dp.live_value == 3
        assert dp.severity == DriftSeverity.HIGH

    def test_detects_missing_key_in_live(self):
        """Key present in baseline but entirely absent from live config."""
        live = {"replicas": 2}
        baseline = {"replicas": 2, "image": "svc:v1.0"}

        report = detect_drift("svc", live, baseline)

        assert report.total_drift_count == 1
        dp = report.drifted_properties[0]
        assert dp.property_path == "image"
        assert dp.iac_value == "svc:v1.0"
        assert dp.live_value is None
        assert dp.severity == DriftSeverity.CRITICAL


# ---------------------------------------------------------------------------
# Nested property drift
# ---------------------------------------------------------------------------

class TestNestedPropertyDrift:
    def test_detects_nested_memory_limit_drift(self):
        live = _simple_live()
        baseline = _simple_baseline()
        baseline["resources"]["limits"]["memory"] = "1Gi"  # IaC says 1Gi, live has 512Mi

        report = detect_drift("payment-service", live, baseline)

        assert report.total_drift_count == 1
        dp = report.drifted_properties[0]
        assert "resources.limits.memory" in dp.property_path
        assert dp.iac_value == "1Gi"
        assert dp.live_value == "512Mi"
        assert dp.severity == DriftSeverity.HIGH

    def test_detects_multiple_nested_drifts(self):
        live = _simple_live()
        baseline = _simple_baseline()
        baseline["resources"]["limits"]["memory"] = "1Gi"
        baseline["resources"]["requests"]["cpu"] = "500m"

        report = detect_drift("payment-service", live, baseline)

        assert report.total_drift_count == 2
        paths = {dp.property_path for dp in report.drifted_properties}
        assert any("limits.memory" in p for p in paths)
        assert any("requests.cpu" in p for p in paths)

    def test_detects_env_drift(self):
        live = _simple_live()
        baseline = _simple_baseline()
        baseline["env"]["TIMEOUT_MS"] = "1000"  # IaC says 1000, live has 5000

        report = detect_drift("payment-service", live, baseline)

        assert report.total_drift_count == 1
        dp = report.drifted_properties[0]
        assert dp.severity == DriftSeverity.HIGH


# ---------------------------------------------------------------------------
# Severity assignment by property_path heuristics
# ---------------------------------------------------------------------------

class TestSeverityHeuristics:
    def _single_drift_report(self, path_key: str, baseline_val, live_val) -> DriftedProperty:
        """Helper: build a drift where the baseline and live differ on path_key."""
        # Construct nested dicts from dot-separated path_key
        def _nest(keys: list[str], value) -> dict:
            if len(keys) == 1:
                return {keys[0]: value}
            return {keys[0]: _nest(keys[1:], value)}

        keys = path_key.split(".")
        baseline = _nest(keys, baseline_val)
        live = _nest(keys, live_val)

        report = detect_drift("svc", live, baseline)
        assert report.total_drift_count == 1, f"Expected 1 drift, got {report.total_drift_count} for path {path_key}"
        return report.drifted_properties[0]

    def test_resources_limits_is_high(self):
        dp = self._single_drift_report("resources.limits.memory", "1Gi", "512Mi")
        assert dp.severity == DriftSeverity.HIGH

    def test_resources_requests_is_medium(self):
        dp = self._single_drift_report("resources.requests.cpu", "500m", "250m")
        assert dp.severity == DriftSeverity.MEDIUM

    def test_replicas_is_high(self):
        dp = self._single_drift_report("replicas", 5, 2)
        assert dp.severity == DriftSeverity.HIGH

    def test_image_is_critical(self):
        dp = self._single_drift_report("image", "svc:v1.0", "svc:v0.9")
        assert dp.severity == DriftSeverity.CRITICAL

    def test_env_is_high(self):
        dp = self._single_drift_report("env.TIMEOUT_MS", "1000", "5000")
        assert dp.severity == DriftSeverity.HIGH

    def test_liveness_probe_is_high(self):
        dp = self._single_drift_report("livenessProbe.initialDelaySeconds", 30, 10)
        assert dp.severity == DriftSeverity.HIGH

    def test_readiness_probe_is_high(self):
        dp = self._single_drift_report("readinessProbe.periodSeconds", 5, 30)
        assert dp.severity == DriftSeverity.HIGH

    def test_hpa_max_replicas_is_medium(self):
        dp = self._single_drift_report("hpa.maxReplicas", 10, 5)
        assert dp.severity == DriftSeverity.MEDIUM

    def test_hpa_min_replicas_is_medium(self):
        dp = self._single_drift_report("hpa.minReplicas", 2, 1)
        assert dp.severity == DriftSeverity.MEDIUM

    def test_annotations_is_low(self):
        dp = self._single_drift_report("annotations.owner", "sre-team", "platform-team")
        assert dp.severity == DriftSeverity.LOW

    def test_unknown_path_defaults_to_medium(self):
        dp = self._single_drift_report("someRandomField", "a", "b")
        assert dp.severity == DriftSeverity.MEDIUM


# ---------------------------------------------------------------------------
# incident_correlation — boost for recent changes
# ---------------------------------------------------------------------------

class TestIncidentCorrelation:
    def test_high_severity_has_base_correlation_0_8(self):
        live = {"replicas": 2}
        baseline = {"replicas": 5}

        report = detect_drift(
            "svc",
            live,
            baseline,
            incident_context={"started_at": "2026-04-19T03:00:00Z"},
        )
        dp = report.drifted_properties[0]
        # No changed_at → no boost, base should be 0.8
        assert dp.incident_correlation == pytest.approx(0.8)

    def test_recent_change_boosts_correlation(self):
        """change within 24 h of incident → +0.15 boost."""
        live = {"replicas": 2}
        baseline = {"replicas": 5}

        incident_context = {
            "started_at": "2026-04-19T03:00:00Z",
            "attribution": {
                "changed_by": "ops-bob",
                "changed_at": "2026-04-18T20:00:00Z",  # 7 h before incident
            },
        }
        report = detect_drift("svc", live, baseline, incident_context=incident_context)
        dp = report.drifted_properties[0]
        # HIGH base = 0.8, boost = 0.15 → 0.95
        assert dp.incident_correlation == pytest.approx(0.95)
        assert dp.changed_by == "ops-bob"
        assert dp.changed_at == "2026-04-18T20:00:00Z"

    def test_old_change_no_boost(self):
        """change more than 24 h before incident → no boost."""
        live = {"replicas": 2}
        baseline = {"replicas": 5}

        incident_context = {
            "started_at": "2026-04-19T03:00:00Z",
            "attribution": {
                "changed_by": "ops-charlie",
                "changed_at": "2026-04-01T10:00:00Z",  # 18 days before
            },
        }
        report = detect_drift("svc", live, baseline, incident_context=incident_context)
        dp = report.drifted_properties[0]
        assert dp.incident_correlation == pytest.approx(0.8)

    def test_low_severity_base_correlation_0_2(self):
        live = {"annotations": {"owner": "sre-team"}}
        baseline = {"annotations": {"owner": "platform-team"}}

        report = detect_drift("svc", live, baseline)
        dp = report.drifted_properties[0]
        assert dp.incident_correlation == pytest.approx(0.2)

    def test_medium_severity_base_correlation_0_5(self):
        live = {"resources": {"requests": {"cpu": "250m"}}}
        baseline = {"resources": {"requests": {"cpu": "500m"}}}

        report = detect_drift("svc", live, baseline)
        dp = report.drifted_properties[0]
        assert dp.incident_correlation == pytest.approx(0.5)

    def test_critical_severity_base_correlation_0_9(self):
        live = {"image": "svc:v0.9"}
        baseline = {"image": "svc:v1.0"}

        report = detect_drift("svc", live, baseline)
        dp = report.drifted_properties[0]
        assert dp.incident_correlation == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# drift_is_likely_root_cause flag
# ---------------------------------------------------------------------------

class TestDriftIsLikelyRootCause:
    def test_high_severity_drift_triggers_root_cause_flag(self):
        live = {"replicas": 2}
        baseline = {"replicas": 5}

        report = detect_drift("svc", live, baseline)
        assert report.drift_is_likely_root_cause is True
        assert report.root_cause_confidence >= 0.8

    def test_low_severity_only_does_not_trigger_root_cause_flag(self):
        live = {"annotations": {"owner": "sre-team"}}
        baseline = {"annotations": {"owner": "platform-team"}}

        report = detect_drift("svc", live, baseline)
        assert report.drift_is_likely_root_cause is False
        assert report.root_cause_confidence == pytest.approx(0.2)

    def test_medium_severity_does_not_trigger_root_cause_flag(self):
        live = {"resources": {"requests": {"cpu": "250m"}}}
        baseline = {"resources": {"requests": {"cpu": "500m"}}}

        report = detect_drift("svc", live, baseline)
        # 0.5 is not > 0.7
        assert report.drift_is_likely_root_cause is False

    def test_critical_severity_triggers_root_cause_flag(self):
        live = {"image": "svc:v0.9"}
        baseline = {"image": "svc:v1.0"}

        report = detect_drift("svc", live, baseline)
        assert report.drift_is_likely_root_cause is True
        assert report.root_cause_confidence >= 0.9

    def test_high_severity_count_reflects_high_and_critical(self):
        live = {"replicas": 2, "image": "svc:v0.9"}
        baseline = {"replicas": 5, "image": "svc:v1.0"}

        report = detect_drift("svc", live, baseline)
        assert report.high_severity_count == 2  # HIGH (replicas) + CRITICAL (image)


# ---------------------------------------------------------------------------
# approve_rollback state transition
# ---------------------------------------------------------------------------

class TestApproveRollback:
    def _make_report(self) -> DriftReport:
        live = {"replicas": 2}
        baseline = {"replicas": 5}
        return detect_drift("payment-service", live, baseline)

    def test_initial_status_is_detected(self):
        report = self._make_report()
        assert report.status == "detected"
        assert report.approved_by is None

    def test_approve_changes_status_to_approved(self):
        report = self._make_report()
        updated = approve_rollback(report, approved_by="sre-alice")
        assert updated.status == "approved"
        assert updated.approved_by == "sre-alice"

    def test_approve_returns_same_object(self):
        report = self._make_report()
        returned = approve_rollback(report, approved_by="sre-alice")
        assert returned is report

    def test_approve_on_reviewing_status_works(self):
        report = self._make_report()
        report.status = "reviewing"
        approve_rollback(report, approved_by="sre-bob")
        assert report.status == "approved"

    def test_approve_on_rolled_back_raises(self):
        report = self._make_report()
        report.status = "rolled_back"
        with pytest.raises(ValueError, match="rolled_back"):
            approve_rollback(report, approved_by="sre-alice")

    def test_approve_on_dismissed_raises(self):
        report = self._make_report()
        report.status = "dismissed"
        with pytest.raises(ValueError, match="dismissed"):
            approve_rollback(report, approved_by="sre-alice")

    def test_requires_human_approval_always_true(self):
        report = self._make_report()
        assert report.requires_human_approval is True
        approve_rollback(report, approved_by="sre-alice")
        assert report.requires_human_approval is True


# ---------------------------------------------------------------------------
# Remediation command generation
# ---------------------------------------------------------------------------

class TestRemediationCommand:
    def _report(self, iac_source: str) -> DriftReport:
        return detect_drift(
            service="payment-service",
            live_config={"replicas": 2},
            iac_baseline={"replicas": 5},
            iac_source=iac_source,
        )

    def test_helm_command(self):
        report = self._report("helm")
        assert "helm upgrade" in report.remediation_command
        assert "payment-service" in report.remediation_command

    def test_terraform_command(self):
        report = self._report("terraform")
        assert "terraform apply" in report.remediation_command
        assert "payment-service" in report.remediation_command

    def test_k8s_manifest_command(self):
        report = self._report("k8s_manifest")
        assert "kubectl apply" in report.remediation_command
        assert "payment-service" in report.remediation_command

    def test_unknown_source_command(self):
        report = self._report("unknown")
        assert "payment-service" in report.remediation_command

    def test_remediation_plan_has_steps(self):
        report = self._report("helm")
        assert isinstance(report.remediation_plan, list)
        assert len(report.remediation_plan) >= 4

    def test_remediation_plan_references_command(self):
        report = self._report("helm")
        plan_text = " ".join(report.remediation_plan)
        # The remediation command should appear somewhere in the plan
        assert "helm upgrade" in plan_text


# ---------------------------------------------------------------------------
# Resource type and name propagation
# ---------------------------------------------------------------------------

class TestResourceMetadata:
    def test_resource_type_and_name_from_incident_context(self):
        incident_context = {
            "resource_type": "configmap",
            "resource_name": "payment-config",
            "started_at": "2026-04-19T03:00:00Z",
        }
        report = detect_drift(
            service="payment-service",
            live_config={"replicas": 2},
            iac_baseline={"replicas": 5},
            incident_context=incident_context,
        )
        dp = report.drifted_properties[0]
        assert dp.resource_type == "configmap"
        assert dp.resource_name == "payment-config"

    def test_defaults_to_deployment_and_service_name(self):
        report = detect_drift(
            service="auth-service",
            live_config={"replicas": 1},
            iac_baseline={"replicas": 3},
        )
        dp = report.drifted_properties[0]
        assert dp.resource_type == "deployment"
        assert dp.resource_name == "auth-service"


# ---------------------------------------------------------------------------
# snapshot_time & service name
# ---------------------------------------------------------------------------

class TestReportMetadata:
    def test_snapshot_time_is_iso8601(self):
        report = detect_drift("svc", {"a": 1}, {"a": 1})
        # Should parse without error
        from datetime import datetime
        datetime.fromisoformat(report.snapshot_time.replace("Z", "+00:00"))

    def test_service_name_propagated(self):
        report = detect_drift("my-service", {"a": 1}, {"a": 2})
        assert report.service == "my-service"

    def test_iac_source_propagated(self):
        report = detect_drift("svc", {"a": 1}, {"a": 2}, iac_source="helm")
        assert report.iac_source == "helm"
