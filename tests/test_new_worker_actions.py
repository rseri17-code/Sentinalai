"""Tests for new worker actions: DevopsWorker (create_fix_pr, rollback, scale)
and ItsmWorker (update_incident).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from workers.devops_worker import DevopsWorker
from workers.itsm_worker import ItsmWorker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_gateway():
    gw = MagicMock()
    gw.invoke.return_value = {"ok": True}
    return gw


# ---------------------------------------------------------------------------
# DevopsWorker — new actions
# ---------------------------------------------------------------------------

class TestCreateFixPr:
    def test_returns_error_when_repo_missing(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        result = worker.execute("create_fix_pr", {"title": "fix: something"})
        assert "error" in result

    def test_returns_error_when_title_missing(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        result = worker.execute("create_fix_pr", {"repo": "org/repo"})
        assert "error" in result

    def test_calls_gateway_with_correct_action(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("create_fix_pr", {
            "repo": "myorg/payment-service",
            "title": "fix: restore null check",
            "body": "## Root cause\n...",
            "patch": "--- a/foo.py\n+++ b/foo.py",
            "base_sha": "abc123",
        })
        gw.invoke.assert_called_once()
        tool_name, action, params = gw.invoke.call_args[0]
        assert action == "create_fix_pr"
        assert params["repo"] == "myorg/payment-service"
        assert params["title"] == "fix: restore null check"

    def test_defaults_actor_id_to_sentinalai(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("create_fix_pr", {
            "repo": "org/repo",
            "title": "fix: test",
        })
        _, _, params = gw.invoke.call_args[0]
        assert params["actor_id"] == "sentinalai"


class TestRollbackDeployment:
    def test_returns_error_when_service_missing(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        result = worker.execute("rollback_deployment", {})
        assert "error" in result

    def test_calls_kubernetes_mcp(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("rollback_deployment", {
            "service": "payment-service",
            "sha": "abc123",
        })
        tool_name, action, params = gw.invoke.call_args[0]
        assert "kubernetes" in tool_name
        assert action == "rollback_deployment"
        assert params["service"] == "payment-service"

    def test_includes_default_kubectl_command(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("rollback_deployment", {"service": "my-svc"})
        _, _, params = gw.invoke.call_args[0]
        assert "kubectl" in params["command"]
        assert "my-svc" in params["command"]

    def test_accepts_custom_command(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        custom = "helm rollback my-release 3"
        worker.execute("rollback_deployment", {
            "service": "my-svc",
            "command": custom,
        })
        _, _, params = gw.invoke.call_args[0]
        assert params["command"] == custom

    def test_defaults_namespace_to_production(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("rollback_deployment", {"service": "svc"})
        _, _, params = gw.invoke.call_args[0]
        assert params["namespace"] == "production"


class TestScaleService:
    def test_returns_error_when_service_missing(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        result = worker.execute("scale_service", {"replicas": 3})
        assert "error" in result

    def test_calls_kubernetes_scale_action(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("scale_service", {"service": "api-server", "replicas": 5})
        tool_name, action, params = gw.invoke.call_args[0]
        assert action == "scale_service"
        assert params["service"] == "api-server"
        assert params["replicas"] == 5

    def test_default_replicas_is_2(self):
        gw = _mock_gateway()
        worker = DevopsWorker(gateway=gw)
        worker.execute("scale_service", {"service": "svc"})
        _, _, params = gw.invoke.call_args[0]
        assert params["replicas"] == 2


# ---------------------------------------------------------------------------
# ItsmWorker — update_incident
# ---------------------------------------------------------------------------

class TestUpdateIncident:
    def test_returns_error_when_incident_id_missing(self):
        worker = ItsmWorker(gateway=_mock_gateway())
        result = worker.execute("update_incident", {})
        assert "error" in result

    def test_calls_servicenow_update_incident(self):
        gw = _mock_gateway()
        worker = ItsmWorker(gateway=gw)
        worker.execute("update_incident", {
            "incident_id": "INC0012345",
            "state": "resolved",
            "resolution_notes": "Fixed by rollback",
        })
        tool_name, action, params = gw.invoke.call_args[0]
        assert "servicenow" in tool_name
        assert action == "update_incident"
        assert params["incident_id"] == "INC0012345"

    def test_passes_all_resolution_fields(self):
        gw = _mock_gateway()
        worker = ItsmWorker(gateway=gw)
        worker.execute("update_incident", {
            "incident_id": "INC001",
            "state": "resolved",
            "resolution_code": "Solved (Permanently)",
            "resolution_notes": "Auto-resolved by SentinalAI",
            "work_notes": "Rollback applied, service stable",
        })
        _, _, params = gw.invoke.call_args[0]
        assert params["state"] == "resolved"
        assert params["resolution_code"] == "Solved (Permanently)"
        assert "SentinalAI" in params["resolution_notes"]
        assert "Rollback" in params["work_notes"]

    def test_partial_update_allowed(self):
        """Should accept minimal params — only incident_id required."""
        gw = _mock_gateway()
        worker = ItsmWorker(gateway=gw)
        result = worker.execute("update_incident", {
            "incident_id": "INC001",
            "work_notes": "Investigating",
        })
        assert "error" not in result

    def test_update_incident_registered(self):
        worker = ItsmWorker(gateway=_mock_gateway())
        assert "update_incident" in worker._handlers


# ---------------------------------------------------------------------------
# DevopsWorker — registration of new actions
# ---------------------------------------------------------------------------

class TestDevopsWorkerRegistration:
    def test_create_fix_pr_registered(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        assert "create_fix_pr" in worker._handlers

    def test_rollback_deployment_registered(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        assert "rollback_deployment" in worker._handlers

    def test_scale_service_registered(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        assert "scale_service" in worker._handlers

    def test_original_actions_still_registered(self):
        worker = DevopsWorker(gateway=_mock_gateway())
        for action in ("get_recent_deployments", "get_pr_details",
                       "get_commit_diff", "get_workflow_runs"):
            assert action in worker._handlers
