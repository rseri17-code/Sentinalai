"""Tests for the stub MCP server."""
from __future__ import annotations

import sys
import os

import pytest

# The stub server lives under scripts/ — add to path for import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# ---------------------------------------------------------------------------
# The stub server functions are importable without starting the server
# ---------------------------------------------------------------------------

from stub_mcp_server import (
    _servicenow,
    _github,
    _splunk,
    _sysdig,
    _dynatrace,
    _moogsoft,
    _confluence,
    _kubernetes,
)


# ---------------------------------------------------------------------------
# ServiceNow stub
# ---------------------------------------------------------------------------

class TestServiceNowStub:
    def test_get_ci_details_returns_ci(self):
        result = _servicenow("get_ci_details", {"service": "payment-service"})
        assert "ci" in result
        assert result["ci"]["name"] == "payment-service"
        assert "dependencies" in result["ci"]

    def test_search_incidents_returns_list(self):
        result = _servicenow("search_incidents", {"service": "payment-service"})
        assert "incidents" in result
        assert isinstance(result["incidents"], list)

    def test_get_change_records_returns_list(self):
        result = _servicenow("get_change_records", {"service": "payment-service"})
        assert "change_records" in result
        assert len(result["change_records"]) > 0
        assert "number" in result["change_records"][0]

    def test_get_known_errors_returns_list(self):
        result = _servicenow("get_known_errors", {"service": "api-gateway"})
        assert "known_errors" in result

    def test_update_incident_returns_updated(self):
        result = _servicenow("update_incident", {
            "incident_id": "INC0012345",
            "state": "resolved",
        })
        assert "updated" in result
        assert result["updated"]["state"] == "resolved"

    def test_unknown_action_returns_empty(self):
        result = _servicenow("nonexistent_action", {})
        assert result == {}


# ---------------------------------------------------------------------------
# GitHub stub
# ---------------------------------------------------------------------------

class TestGitHubStub:
    def test_get_recent_deployments_returns_deployments(self):
        result = _github("get_recent_deployments", {"service": "payment-service"})
        assert "deployments" in result
        assert len(result["deployments"]) > 0
        dep = result["deployments"][0]
        assert "sha" in dep
        assert "merged_at" in dep

    def test_get_pr_details_returns_pr(self):
        result = _github("get_pr_details", {"repo": "myorg/svc", "pr_number": 847})
        assert "pr" in result
        assert "files_changed" in result["pr"]

    def test_get_commit_diff_returns_diff(self):
        result = _github("get_commit_diff", {"repo": "myorg/svc", "sha": "abc123"})
        assert "commit" in result
        assert "files" in result["commit"]
        assert len(result["commit"]["files"]) > 0
        assert "patch" in result["commit"]["files"][0]

    def test_get_workflow_runs_returns_runs(self):
        result = _github("get_workflow_runs", {"service": "api-gateway"})
        assert "workflow_runs" in result

    def test_create_fix_pr_returns_pr_number(self):
        result = _github("create_fix_pr", {
            "repo": "myorg/payment-service",
            "title": "fix: restore null check",
        })
        assert "pr" in result
        assert "number" in result["pr"]
        assert "html_url" in result["pr"]


# ---------------------------------------------------------------------------
# Splunk stub
# ---------------------------------------------------------------------------

class TestSplunkStub:
    def test_search_logs_returns_logs(self):
        result = _splunk("search_logs", {"service": "checkout-service"})
        assert "logs" in result
        assert len(result["logs"]) > 0

    def test_search_oneshot_returns_logs(self):
        result = _splunk("search_oneshot", {"service": "api-gateway"})
        assert "logs" in result

    def test_log_entries_have_required_fields(self):
        result = _splunk("search_logs", {"service": "svc"})
        for log in result["logs"]:
            assert "level" in log
            assert "message" in log

    def test_get_change_data_returns_changes(self):
        result = _splunk("get_change_data", {"service": "payment-service"})
        assert "change_data" in result


# ---------------------------------------------------------------------------
# Sysdig stub
# ---------------------------------------------------------------------------

class TestSysdigStub:
    def test_get_service_metrics_returns_metrics(self):
        result = _sysdig("get_service_metrics", {"service": "payment-service"})
        assert "metrics" in result
        m = result["metrics"]
        assert "error_rate" in m
        assert "latency_p95" in m or "p95_ms" in m

    def test_get_golden_signals_has_error_rate(self):
        result = _sysdig("get_golden_signals", {"service": "svc"})
        assert "metrics" in result
        assert result["metrics"]["error_rate"] > 0

    def test_get_kubernetes_events_returns_events(self):
        result = _sysdig("get_kubernetes_events", {"service": "api-server"})
        assert "events" in result


# ---------------------------------------------------------------------------
# Dynatrace stub
# ---------------------------------------------------------------------------

class TestDynatraceStub:
    def test_get_problems_returns_problems(self):
        result = _dynatrace("get_problems", {"service": "checkout"})
        assert "problems" in result
        assert len(result["problems"]) > 0

    def test_get_error_samples_returns_errors(self):
        result = _dynatrace("get_error_samples", {"service": "payment-service"})
        assert "errors" in result
        for err in result["errors"]:
            assert "message" in err
            assert "stack_trace" in err


# ---------------------------------------------------------------------------
# Kubernetes stub
# ---------------------------------------------------------------------------

class TestKubernetesStub:
    def test_rollback_deployment_returns_success(self):
        result = _kubernetes("rollback_deployment", {"service": "payment-service"})
        assert "rollback" in result
        assert result["rollback"]["status"] == "success"
        assert "payment-service" in result["rollback"]["deployment"]

    def test_scale_service_returns_success(self):
        result = _kubernetes("scale_service", {
            "service": "api-server",
            "replicas": 5,
        })
        assert "scale" in result
        assert result["scale"]["replicas"] == 5

    def test_rollback_defaults_to_production_namespace(self):
        result = _kubernetes("rollback_deployment", {"service": "svc"})
        assert result["rollback"]["namespace"] == "production"


# ---------------------------------------------------------------------------
# Confluence stub
# ---------------------------------------------------------------------------

class TestConfluenceStub:
    def test_search_runbooks_returns_runbooks(self):
        result = _confluence("search_runbooks", {"service": "payment-service"})
        assert "runbooks" in result
        assert len(result["runbooks"]) > 0

    def test_search_postmortems_returns_postmortems(self):
        result = _confluence("search_postmortems", {"service": "payment-service"})
        assert "postmortems" in result
        assert "root_cause" in result["postmortems"][0]
