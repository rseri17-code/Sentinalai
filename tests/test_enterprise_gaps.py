"""Tests for the 6 enterprise gap-filling modules.

Coverage:
  1. supervisor/alert_dedup.py  — fingerprint, cooldown, dedup, thread-safety
  2. integrations/tenant_config.py — get/upsert/threshold resolution
  3. supervisor/cold_start_seeder.py — seeding idempotency and count
  4. supervisor/k8s_executor.py — dry-run enforcement, approval gate, audit
  5. supervisor/postmortem_generator.py — report structure, markdown, approval
  6. agui/api/intake.py — OpsGenie/Grafana/CloudWatch normalizers
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

import pytest


# ── 1. Alert Deduplication ─────────────────────────────────────────────────────

class TestAlertDedup:
    @pytest.fixture()
    def dedup(self, tmp_path):
        from supervisor.alert_dedup import AlertDeduplicator
        return AlertDeduplicator(db_path=str(tmp_path / "dedup.db"))

    def test_fingerprint_stable(self, dedup):
        fp1 = dedup.fingerprint("INC-1", "api-gw", 2, ["prod", "high"])
        fp2 = dedup.fingerprint("INC-1", "api-gw", 2, ["high", "prod"])  # tag order shouldn't matter
        assert fp1 == fp2

    def test_fingerprint_changes_on_different_service(self, dedup):
        fp1 = dedup.fingerprint("INC-1", "api-gw", 2, [])
        fp2 = dedup.fingerprint("INC-1", "other-svc", 2, [])
        assert fp1 != fp2

    def test_fingerprint_changes_on_different_severity_tier(self, dedup):
        # sev 1 and 2 are both "critical" → same tier
        fp1 = dedup.fingerprint("INC-1", "svc", 1, [])
        fp2 = dedup.fingerprint("INC-1", "svc", 2, [])
        assert fp1 == fp2
        # sev 3 is "medium" → different
        fp3 = dedup.fingerprint("INC-1", "svc", 3, [])
        assert fp1 != fp3

    def test_first_check_is_not_duplicate(self, dedup):
        result = dedup.check_and_register("INC-100", "svc", 3, [], "inv-001")
        assert result.is_duplicate is False
        assert result.fingerprint != ""

    def test_second_check_within_cooldown_is_duplicate(self, dedup):
        dedup.check_and_register("INC-200", "svc", 3, [], "inv-first")
        result = dedup.check_and_register("INC-200", "svc", 3, [], "inv-second")
        assert result.is_duplicate is True
        assert result.existing_investigation_id == "inv-first"
        assert result.cooldown_remaining_secs > 0

    def test_different_incidents_same_fingerprint_dedup(self, dedup):
        """Different incident IDs but same service+severity+tags → deduplicated."""
        dedup.check_and_register("INC-A", "payment", 1, ["prod"], "inv-a")
        result = dedup.check_and_register("INC-B", "payment", 1, ["prod"], "inv-b")
        assert result.is_duplicate is True

    def test_correlated_ids_returned(self, dedup):
        dedup.check_and_register("INC-X", "svc", 3, [], "inv-x")
        result = dedup.check_and_register("INC-Y", "other-svc", 3, [], "inv-y")
        # Different service → no correlation
        assert result.is_duplicate is False

    def test_stats_count_registrations(self, dedup):
        dedup.check_and_register("INC-1", "svc", 3, [], "inv-1")
        dedup.check_and_register("INC-2", "svc2", 3, [], "inv-2")
        stats = dedup.get_stats()
        assert stats["total_registered"] >= 2

    def test_thread_safe_concurrent_checks(self, dedup):
        errors = []
        results = []
        lock = threading.Lock()

        def register(i):
            try:
                r = dedup.check_and_register(f"INC-{i}", "svc", 3, [], f"inv-{i}")
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=register, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 10

    def test_expire_old_entries(self, dedup):
        dedup.check_and_register("INC-OLD", "svc", 3, [], "inv-old")
        deleted = dedup.expire_old_entries(max_age_secs=0)  # expire everything
        assert deleted >= 1


# ── 2. Tenant Config ────────────────────────────────────────────────────────────

class TestTenantConfig:
    def test_default_tenant_always_returns_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        # Reset module state
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        cfg = tc.get_tenant_config("default")
        assert cfg.org_id == "default"

    def test_upsert_and_retrieve(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        tc.upsert_tenant("acme", {"slack_channel": "#acme-sre", "enable_k8s_actions": True})
        cfg = tc.get_tenant_config("acme")
        assert cfg.slack_channel == "#acme-sre"
        assert cfg.enable_k8s_actions is True

    def test_threshold_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        cfg = tc.get_tenant_config("no-overrides")
        val = cfg.threshold("critique_threshold")
        assert 0.0 < val < 1.0

    def test_threshold_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        tc.upsert_tenant("override-org", {"threshold_overrides": {"critique_threshold": 0.99}})
        cfg = tc.get_tenant_config("override-org")
        assert cfg.threshold("critique_threshold") == pytest.approx(0.99)

    def test_allows_service_empty_means_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        cfg = tc.get_tenant_config("open-org")
        assert cfg.allows_service("any-service") is True

    def test_allows_service_restricted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        tc.upsert_tenant("restricted", {"allowed_services": ["api-gateway", "payment"]})
        cfg = tc.get_tenant_config("restricted")
        assert cfg.allows_service("api-gateway") is True
        assert cfg.allows_service("unknown-svc") is False

    def test_to_dict_does_not_expose_raw_pd_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        tc.upsert_tenant("secure-org", {"pagerduty_service_key": "secret-key-123"})
        cfg = tc.get_tenant_config("secure-org")
        d = cfg.to_dict()
        # to_dict masks the key value with a boolean
        assert d["pagerduty_service_key"] is True  # masked to bool
        assert "secret-key-123" not in json.dumps(d)

    def test_list_tenants(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TENANT_CONFIG_PATH", str(tmp_path / "tenants.json"))
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        tc.upsert_tenant("org-a", {})
        tc.upsert_tenant("org-b", {})
        tenants = tc.list_tenants()
        assert "org-a" in tenants
        assert "org-b" in tenants

    def test_config_persisted_to_disk(self, tmp_path, monkeypatch):
        path = str(tmp_path / "tenants.json")
        monkeypatch.setenv("TENANT_CONFIG_PATH", path)
        import integrations.tenant_config as tc
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        tc.upsert_tenant("disk-org", {"slack_channel": "#disk-test"})

        # Re-load from disk
        tc._loaded = False
        tc._cache.clear()
        tc._raw.clear()

        cfg = tc.get_tenant_config("disk-org")
        assert cfg.slack_channel == "#disk-test"


# ── 3. Cold-Start Seeder ────────────────────────────────────────────────────────

class TestColdStartSeeder:
    def test_seed_returns_counts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SEED_EXAMPLES_PER_ARCHETYPE", "1")
        monkeypatch.setenv("SEED_MARKER_DIR", str(tmp_path))

        from supervisor.cold_start_seeder import seed_tenant, _ARCHETYPES
        result = seed_tenant(org_id="test-org", force=True)

        assert "seeded" in result
        assert "skipped" in result
        # At minimum the seeder attempted all archetypes
        assert result["seeded"] + result["skipped"] >= len(_ARCHETYPES)

    def test_seed_idempotent_without_force(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SEED_EXAMPLES_PER_ARCHETYPE", "1")
        monkeypatch.setenv("SEED_MARKER_DIR", str(tmp_path))

        from supervisor.cold_start_seeder import seed_tenant
        r1 = seed_tenant(org_id="idem-org", force=True)
        r2 = seed_tenant(org_id="idem-org", force=False)

        # Second call should skip everything
        assert r2["seeded"] == 0

    def test_different_orgs_seed_independently(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SEED_EXAMPLES_PER_ARCHETYPE", "1")
        monkeypatch.setenv("SEED_MARKER_DIR", str(tmp_path))

        from supervisor.cold_start_seeder import seed_tenant
        r1 = seed_tenant(org_id="org-alpha", force=True)
        r2 = seed_tenant(org_id="org-beta", force=True)

        assert r1["seeded"] > 0
        assert r2["seeded"] > 0

    def test_archetypes_produce_unique_fingerprints(self):
        from supervisor.cold_start_seeder import _ARCHETYPES
        types = [a["incident_type"] for a in _ARCHETYPES]
        assert len(types) == len(set(types)), "archetype incident_types must be unique"

    def test_seeder_tolerates_missing_experience_store(self, tmp_path, monkeypatch):
        """Seeder must complete even if ExperienceStore import fails."""
        monkeypatch.setenv("SEED_EXAMPLES_PER_ARCHETYPE", "1")
        monkeypatch.setenv("SEED_MARKER_DIR", str(tmp_path))
        monkeypatch.setattr(
            "supervisor.cold_start_seeder._seed_calibration_bins", lambda: None
        )
        from supervisor.cold_start_seeder import seed_tenant
        result = seed_tenant(org_id="tolerant-org", force=True)
        assert isinstance(result, dict)


# ── 4. K8s Executor ────────────────────────────────────────────────────────────

class TestK8sExecutor:
    @pytest.fixture()
    def executor(self):
        from supervisor.k8s_executor import K8sExecutor
        return K8sExecutor()

    def test_dry_run_is_default(self):
        import supervisor.k8s_executor as k8s_mod
        assert k8s_mod.K8S_DRY_RUN is True

    def test_build_rollback_command(self, executor):
        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="rollback_deployment", namespace="prod", resource_name="api-gw")
        cmd = executor._build_command(action, dry_run=True)
        assert "rollout" in cmd
        assert "undo" in cmd
        assert "deployment/api-gw" in cmd
        assert "--dry-run=server" in cmd

    def test_build_scale_command(self, executor):
        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="scale_deployment", namespace="prod", resource_name="frontend", replicas=5)
        cmd = executor._build_command(action, dry_run=False)
        assert "scale" in cmd
        assert "--replicas=5" in cmd
        assert "--dry-run=server" not in cmd

    def test_unknown_action_raises(self, executor):
        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="nuke_everything", resource_name="*")
        with pytest.raises(ValueError, match="Unknown action"):
            executor._build_command(action, dry_run=True)

    def test_missing_resource_name_raises(self, executor):
        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="rollback_deployment", namespace="prod", resource_name="")
        with pytest.raises(ValueError, match="resource_name required"):
            executor._build_command(action, dry_run=True)

    def test_namespace_allow_list_blocks_forbidden_ns(self, monkeypatch):
        monkeypatch.setenv("K8S_ALLOWED_NAMESPACES", "staging,dev")
        import importlib
        import supervisor.k8s_executor as mod
        importlib.reload(mod)

        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="rollback_deployment", namespace="production", resource_name="api")
        # Must be rejected — 'production' not in allowed list
        executor = mod.K8sExecutor()
        result = executor.execute(action)
        assert result.ok is False
        assert "not in K8S_ALLOWED_NAMESPACES" in result.error

        # Restore original env
        monkeypatch.delenv("K8S_ALLOWED_NAMESPACES", raising=False)
        importlib.reload(mod)

    def test_approval_token_lifecycle(self, executor):
        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="rollback_deployment", namespace="prod", resource_name="api")
        token = executor.request_approval(action)
        assert len(token) == 36  # UUID format
        approved = executor.approve(token)
        assert approved is True

    def test_invalid_approval_token_rejected(self, executor):
        result = executor._validate_approval("bad-token", None)
        assert result is False

    def test_execute_kubectl_not_found_returns_error(self, executor, monkeypatch):
        """When kubectl isn't installed, result.ok=False with descriptive error."""
        monkeypatch.setenv("K8S_KUBECTL_PATH", "/nonexistent/kubectl")
        monkeypatch.setattr("supervisor.k8s_executor.K8S_DRY_RUN", True)
        from supervisor.k8s_executor import K8sAction
        action = K8sAction(action="rollback_deployment", namespace="prod", resource_name="api")
        result = executor.execute(action)
        assert result.ok is False
        assert "kubectl" in result.error.lower()


# ── 5. Postmortem Generator ────────────────────────────────────────────────────

class TestPostmortemGenerator:
    @pytest.fixture()
    def minimal_rca(self):
        return {
            "incident_id": "INC-9001",
            "affected_service": "checkout-service",
            "severity_label": "critical",
            "root_cause": "Deploy of checkout-service v1.2.3 introduced a missing DB index on orders table causing full-table scan at 2000 RPS.",
            "fix_applied": "Rollback to v1.2.2; add composite index on (user_id, created_at)",
            "confidence": 82.0,
            "confidence_calibrated": 80,
            "start_time": "2026-01-15T09:00:00Z",
            "evidence_timeline": [
                {"timestamp": "2026-01-15T09:01:00Z", "description": "P95 latency spiked to 8s", "source": "sysdig"},
                {"timestamp": "2026-01-15T09:03:00Z", "description": "Error rate hit 15%", "source": "splunk"},
            ],
            "rounds_run": 2,
            "stuck": False,
            "experience_stored": True,
            "experience_matches": 1,
            "elapsed_ms": 12000.0,
        }

    def test_report_has_all_required_fields(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T09:45:00Z")

        assert report.report_id != ""
        assert report.incident_id == "INC-9001"
        assert report.affected_service == "checkout-service"
        assert report.severity == "critical"
        assert report.status == "draft"
        assert report.reviewed_by is None

    def test_duration_computed_correctly(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T09:45:00Z")
        assert report.duration_minutes == pytest.approx(45.0, abs=1.0)

    def test_executive_summary_contains_service(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        assert "checkout-service" in report.executive_summary

    def test_timeline_includes_start_and_resolved(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        descs = " ".join(e.get("description", "") for e in report.timeline)
        assert "detected" in descs.lower() or "fired" in descs.lower()
        assert "resolved" in descs.lower() or "restored" in descs.lower()

    def test_five_whys_has_five_entries(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        assert len(report.five_whys) == 5

    def test_action_items_not_empty(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        assert len(report.action_items) >= 2

    def test_action_items_have_required_fields(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        for ai in report.action_items:
            assert ai.title
            assert ai.priority in ("P1", "P2", "P3")
            assert ai.due_days > 0

    def test_approve_changes_status(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca)
        assert report.status == "draft"
        report.approve("bob")
        assert report.status == "approved"
        assert report.reviewed_by == "bob"

    def test_to_markdown_contains_key_sections(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        md = report.to_markdown()

        assert "# Postmortem" in md
        assert "## Executive Summary" in md
        assert "## Timeline" in md
        assert "## Five Whys" in md
        assert "## Action Items" in md

    def test_to_markdown_renders_action_table(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca, resolved_at="2026-01-15T10:00:00Z")
        md = report.to_markdown()
        assert "| Priority |" in md

    def test_empty_rca_does_not_raise(self):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem({})
        assert report.incident_id == "UNKNOWN"
        assert report.status == "draft"

    def test_similar_incidents_included(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(
            minimal_rca,
            similar_incidents=["INC-1000", "INC-2000"],
        )
        assert "INC-1000" in report.similar_past_incidents

    def test_team_notes_appear_in_timeline(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        note = "Team noticed elevated latency 2 minutes before alert"
        report = generate_postmortem(minimal_rca, team_notes=[note])
        descs = " ".join(e.get("description", "") for e in report.timeline)
        assert note in descs

    def test_what_went_well_not_empty(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca)
        assert len(report.what_went_well) >= 1

    def test_prevention_recommendations_not_empty(self, minimal_rca):
        from supervisor.postmortem_generator import generate_postmortem
        report = generate_postmortem(minimal_rca)
        assert len(report.prevention_recommendations) >= 1


# ── 6. Intake normalizers (OpsGenie / Grafana / CloudWatch) ────────────────────

class TestIntakeNormalizers:
    """Unit-test the normalizer helpers without spinning up FastAPI."""

    def _opsgenie_alert(self, priority="P2", entity="payment-svc"):
        return {
            "alertId": "og-alert-001",
            "message": "Payment service error rate elevated",
            "entity": entity,
            "priority": priority,
            "tags": ["prod", "payment"],
            "description": "Error rate above 5% threshold",
        }

    def _grafana_unified_alert(self):
        return {
            "fingerprint": "abc123fingerprint",
            "status": "firing",
            "labels": {"alertname": "HighLatency", "service": "api-gateway", "severity": "critical"},
            "annotations": {"summary": "API gateway p95 latency above 2s"},
            "startsAt": "2026-01-15T09:00:00Z",
        }

    def _cloudwatch_alarm(self):
        return {
            "AlarmName": "High-CPU-Usage-prod",
            "AlarmDescription": "CPU usage above 90%",
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "Region": "us-east-1",
            "Trigger": {
                "Namespace": "AWS/EC2",
                "Dimensions": [{"name": "InstanceId", "value": "i-12345"}],
            },
        }

    def test_opsgenie_normalizer_priority_p1_maps_sev1(self):
        from agui.api.intake import _opsgenie_to_incident
        inc = _opsgenie_to_incident(self._opsgenie_alert(priority="P1"))
        assert inc.severity == 1

    def test_opsgenie_normalizer_priority_p3_maps_sev3(self):
        from agui.api.intake import _opsgenie_to_incident
        inc = _opsgenie_to_incident(self._opsgenie_alert(priority="P3"))
        assert inc.severity == 3

    def test_opsgenie_normalizer_source_is_opsgenie(self):
        from agui.api.intake import _opsgenie_to_incident
        inc = _opsgenie_to_incident(self._opsgenie_alert())
        assert inc.source == "opsgenie"

    def test_opsgenie_normalizer_tags_preserved(self):
        from agui.api.intake import _opsgenie_to_incident
        inc = _opsgenie_to_incident(self._opsgenie_alert())
        assert "prod" in inc.tags
        assert "payment" in inc.tags

    def test_opsgenie_normalizer_entity_becomes_service(self):
        from agui.api.intake import _opsgenie_to_incident
        inc = _opsgenie_to_incident(self._opsgenie_alert(entity="checkout-svc"))
        assert inc.affected_service == "checkout-svc"

    def test_grafana_unified_normalizer_source(self):
        from agui.api.intake import _grafana_unified_to_incident
        inc = _grafana_unified_to_incident(self._grafana_unified_alert(), {})
        assert inc.source == "grafana"

    def test_grafana_unified_normalizer_severity_critical(self):
        from agui.api.intake import _grafana_unified_to_incident
        inc = _grafana_unified_to_incident(self._grafana_unified_alert(), {})
        assert inc.severity == 1  # critical → 1

    def test_grafana_unified_normalizer_fingerprint_as_id(self):
        from agui.api.intake import _grafana_unified_to_incident
        inc = _grafana_unified_to_incident(self._grafana_unified_alert(), {})
        assert inc.incident_id == "abc123fingerprint"

    def test_grafana_unified_normalizer_service_from_labels(self):
        from agui.api.intake import _grafana_unified_to_incident
        inc = _grafana_unified_to_incident(self._grafana_unified_alert(), {})
        assert inc.affected_service == "api-gateway"

    def test_cloudwatch_normalizer_source(self):
        from agui.api.intake import _cloudwatch_to_incident
        inc = _cloudwatch_to_incident(self._cloudwatch_alarm())
        assert inc.source == "cloudwatch"

    def test_cloudwatch_normalizer_alarm_name_in_incident_id(self):
        from agui.api.intake import _cloudwatch_to_incident
        inc = _cloudwatch_to_incident(self._cloudwatch_alarm())
        assert "High-CPU-Usage-prod" in inc.incident_id

    def test_cloudwatch_normalizer_summary_starts_with_alarm(self):
        from agui.api.intake import _cloudwatch_to_incident
        inc = _cloudwatch_to_incident(self._cloudwatch_alarm())
        assert inc.summary.startswith("CloudWatch ALARM:")

    def test_cloudwatch_normalizer_namespace_in_tags(self):
        from agui.api.intake import _cloudwatch_to_incident
        inc = _cloudwatch_to_incident(self._cloudwatch_alarm())
        assert any("AWS/EC2" in tag for tag in inc.tags)

    def test_cloudwatch_critical_namespace_gets_sev2(self):
        from agui.api.intake import _cloudwatch_to_incident
        alarm = self._cloudwatch_alarm()
        alarm["Trigger"]["Namespace"] = "AWS/RDS"
        inc = _cloudwatch_to_incident(alarm)
        assert inc.severity == 2

    def test_opsgenie_missing_alert_id_gets_uuid(self):
        from agui.api.intake import _opsgenie_to_incident
        alert = {"message": "test", "priority": "P3", "entity": "svc"}
        inc = _opsgenie_to_incident(alert)
        assert inc.incident_id != ""
        assert len(inc.incident_id) > 0

    def test_grafana_legacy_normalizer(self):
        from agui.api.intake import _grafana_legacy_to_incident
        payload = {
            "ruleId": 42,
            "ruleName": "CPU High",
            "state": "alerting",
            "message": "CPU over threshold",
            "evalMatches": [{"tags": {"service": "worker-svc"}}],
        }
        inc = _grafana_legacy_to_incident(payload)
        assert inc.source == "grafana"
        assert inc.incident_id == "42"
        assert inc.affected_service == "worker-svc"
