"""Offline tests for the OSS validation MCP gateway.

No live network. HTTP backends are faked via an in-memory transport.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oss_validation_gateway.backends import Backends, Settings
from oss_validation_gateway.dispatch import dispatch
from oss_validation_gateway.names import (
    DEFAULT_TARGETS,
    gateway_tool_name,
    parse_tool_name,
    target_for_server,
    tool_catalog,
)
from oss_validation_gateway.protocol import handle_rpc
from oss_validation_gateway.queries import metric_hint_to_promql, splunk_query_to_logql
from oss_validation_gateway.server import create_app
from oss_validation_gateway.shaping import (
    incident_id_of,
    shape_golden_signals,
    shape_incident,
    shape_logs,
    shape_metrics,
)
from supervisor.playbook_loader import CANONICAL_WORKERS, load_yaml_playbooks
from workers.mcp_client import _to_gateway_tool_name

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        self.calls.append((method, url, params))
        matches = [(prefix, payload) for prefix, payload in self.routes.items() if prefix in url]
        if matches:
            _prefix, payload = max(matches, key=lambda item: len(item[0]))
            if callable(payload):
                return payload(method, url, params, body)
            return payload
        raise ConnectionError(f"no fake route for {url}")


def _alert(incident_id: str = "INC-OSS-001", service: str = "payment-service") -> dict[str, Any]:
    return {
        "fingerprint": "fp-oss-001",
        "status": {"state": "active"},
        "startsAt": "2026-09-21T12:00:00.000Z",
        "labels": {
            "alertname": "PaymentServiceTimeout",
            "service": service,
            "incident_id": incident_id,
            "severity": "critical",
        },
        "annotations": {
            "summary": f"{service} request timeout — upstream payment-db not responding",
            "description": "p95 latency 2500ms vs baseline 80ms",
            "incident_id": incident_id,
        },
    }


def _loki_payload(line: str = "ERROR timeout waiting for connection: payment-db") -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "payment-service", "job": "oss-demo", "level": "error"},
                    "values": [["1705322411000000000", line]],
                }
            ],
        },
    }


def _prom_vector(value: float, service: str = "payment-service") -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"service": service},
                    "value": [1705322411, str(value)],
                }
            ],
        },
    }


def _prom_range(values: list[float], service: str = "payment-service") -> dict[str, Any]:
    pairs = [[1705322400 + i * 30, str(v)] for i, v in enumerate(values)]
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"service": service}, "values": pairs}],
        },
    }


def _backends(routes: dict[str, Any] | None = None, **setting_kw: Any) -> Backends:
    settings = Settings(
        prometheus_url="http://prom.example",
        loki_url="http://loki.example",
        alertmanager_url="http://am.example",
        timeout_seconds=1.0,
        **setting_kw,
    )
    return Backends(settings=settings, transport=FakeTransport(routes))


# ---------------------------------------------------------------------------
# Name mapping
# ---------------------------------------------------------------------------

class TestNameMapping:
    def test_defaults_match_mcp_client(self):
        assert gateway_tool_name("splunk", "search_oneshot") == _to_gateway_tool_name(
            "splunk.search_oneshot"
        )
        assert gateway_tool_name("moogsoft", "get_incident_by_id") == _to_gateway_tool_name(
            "moogsoft.get_incident_by_id"
        )
        assert gateway_tool_name("sysdig", "query_metrics") == _to_gateway_tool_name(
            "sysdig.query_metrics"
        )
        assert gateway_tool_name("kubernetes", "get_pod_logs") == _to_gateway_tool_name(
            "kubernetes.get_pod_logs"
        )

    def test_parse_triple_underscore(self):
        assert parse_tool_name("SplunkTarget___search_oneshot") == ("splunk", "search_oneshot")
        assert parse_tool_name("MoogsoftTarget___get_incident_by_id") == (
            "moogsoft",
            "get_incident_by_id",
        )

    def test_parse_dotted(self):
        assert parse_tool_name("splunk.search_oneshot") == ("splunk", "search_oneshot")
        assert parse_tool_name("sysdig.get_kubernetes_events") == (
            "sysdig",
            "get_kubernetes_events",
        )

    def test_parse_unknown_returns_none(self):
        assert parse_tool_name("") is None
        assert parse_tool_name("not-a-tool") is None
        assert parse_tool_name("UnknownTarget___foo") is None

    def test_target_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_TARGET_SPLUNK", "AcmeLogs")
        assert target_for_server("splunk") == "AcmeLogs"
        assert parse_tool_name("AcmeLogs___search_oneshot") == ("splunk", "search_oneshot")
        assert gateway_tool_name("splunk", "search_oneshot") == "AcmeLogs___search_oneshot"

    def test_catalog_uses_agentcore_names(self):
        names = {t["name"] for t in tool_catalog()}
        assert "MoogsoftTarget___get_incident_by_id" in names
        assert "SplunkTarget___search_oneshot" in names
        assert "SysdigTarget___query_metrics" in names
        assert "KubernetesTarget___get_deployment_status" in names
        for server, target in DEFAULT_TARGETS.items():
            assert any(n.startswith(f"{target}___") for n in names), server


# ---------------------------------------------------------------------------
# Query translation
# ---------------------------------------------------------------------------

class TestQueryTranslation:
    def test_timeout_hint_to_logql(self):
        logql = splunk_query_to_logql("timeout payment-service", "payment-service")
        assert 'service="payment-service"' in logql
        assert '|= "timeout"' in logql

    def test_empty_query_selects_service(self):
        logql = splunk_query_to_logql("", "api-gateway")
        assert logql == '{service="api-gateway"}'

    def test_unsafe_service_falls_back(self):
        logql = splunk_query_to_logql("timeout", 'pay"ment')
        assert "payment-service" in logql

    def test_metric_hint_promql(self):
        assert "demo_latency_p95_ms" in metric_hint_to_promql("response_time_ms", "payment-service")
        assert "process_resident_memory_bytes" in metric_hint_to_promql(
            "memory_usage_bytes", "payment-service"
        )


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

class TestShaping:
    def test_incident_has_required_fields(self):
        incident = shape_incident(_alert())
        assert incident["incident_id"] == "INC-OSS-001"
        assert incident["summary"]
        assert "timeout" in incident["summary"].lower()
        assert incident["affected_service"] == "payment-service"
        assert incident["severity"] == 1
        assert incident["status"] == "open"

    def test_incident_id_prefers_label(self):
        assert incident_id_of(_alert()) == "INC-OSS-001"

    def test_logs_splunk_shape(self):
        shaped = shape_logs(_loki_payload())
        results = shaped["logs"]["results"]
        assert shaped["logs"]["count"] == 1
        row = results[0]
        assert "timeout" in row["message"]
        assert row["_raw"] == row["message"]
        assert row["_time"]
        assert row["downstream"] == "payment-db"
        assert row["level"] == "ERROR"

    def test_metrics_nested_list(self):
        shaped = shape_metrics(_prom_range([80, 100, 2500]), metric_name="response_time_ms")
        inner = shaped["metrics"]
        assert "metrics" in inner
        assert len(inner["metrics"]) == 3
        assert inner["metrics"][-1]["value"] == 2500

    def test_golden_signals_latency_keys(self):
        shaped = shape_golden_signals(
            {"latency_p95": 2500, "latency_baseline_p95": 80, "error_rate": 0.12},
            service="payment-service",
        )
        gs = shaped["signals"]["golden_signals"]
        assert gs["latency"]["p95"] == 2500
        assert gs["latency"]["baseline_p95"] == 80
        assert gs["errors"]["rate"] == 0.12


# ---------------------------------------------------------------------------
# Dispatch (fake HTTP)
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_get_incident_by_id(self):
        gw = _backends({"/api/v2/alerts": [_alert()]})
        result = dispatch("MoogsoftTarget___get_incident_by_id", {"incident_id": "INC-OSS-001"}, gw)
        assert result["incident"]["incident_id"] == "INC-OSS-001"
        assert result["incident"]["affected_service"] == "payment-service"

    def test_get_incident_missing(self):
        gw = _backends({"/api/v2/alerts": [_alert()]})
        result = dispatch("moogsoft.get_incident_by_id", {"incident_id": "NOPE"}, gw)
        assert result["incident"] is None
        assert result["error"] == "incident_not_found"

    def test_search_oneshot_loki(self):
        gw = _backends({"/loki/api/v1/query_range": _loki_payload()})
        result = dispatch(
            "SplunkTarget___search_oneshot",
            {"query": "timeout payment-service", "service": "payment-service"},
            gw,
        )
        assert result["logs"]["count"] == 1
        assert "timeout" in result["logs"]["results"][0]["message"]
        assert '|= "timeout"' in result["logql"]

    def test_query_metrics_prometheus(self):
        gw = _backends({"/api/v1/query_range": _prom_range([80.0, 2500.0])})
        result = dispatch(
            "SysdigTarget___query_metrics",
            {"service": "payment-service", "metric": "response_time_ms"},
            gw,
        )
        points = result["metrics"]["metrics"]
        assert points[-1]["value"] == 2500.0
        assert "demo_latency_p95_ms" in result["promql"]

    def test_golden_signals_from_dynatrace_name(self):
        gw = _backends({"/api/v1/query": _prom_vector(2500.0)})
        result = dispatch(
            "DynatraceTarget___get_metrics",
            {"service": "payment-service"},
            gw,
        )
        assert "golden_signals" in result["signals"]
        assert result["signals"]["golden_signals"]["latency"]["p95"] == 2500.0

    def test_change_data_is_empty_not_fabricated(self):
        gw = _backends({})
        result = dispatch("splunk.get_change_data", {"service": "payment-service"}, gw)
        assert result["changes"] == []
        assert result["skipped"] is True

    def test_github_does_not_fabricate_pr(self):
        gw = _backends({})
        result = dispatch("GitHubTarget___create_fix_pr", {"title": "fix"}, gw)
        assert result["created"] is False
        assert result["pr"] is None
        assert "github" in result["error"]

    def test_servicenow_skipped(self):
        gw = _backends({})
        result = dispatch("servicenow.get_ci_details", {"service": "payment-service"}, gw)
        assert result["skipped"] is True
        assert result["ci"] == {}

    def test_kubernetes_mutations_disabled(self):
        gw = _backends({})
        result = dispatch(
            "KubernetesTarget___rollback_deployment",
            {"service": "payment-service"},
            gw,
        )
        assert result["skipped"] is True
        assert result["rollback"]["status"] == "skipped"

    def test_kubernetes_status_without_cluster(self):
        gw = _backends({})
        result = dispatch(
            "kubernetes.get_deployment_status",
            {"service": "payment-service"},
            gw,
        )
        assert result["error"] == "kubernetes_not_configured"
        assert result["available"] is False

    def test_backend_down_fail_open_logs(self):
        gw = _backends({})  # no loki route → ConnectionError
        result = dispatch("splunk.search_oneshot", {"query": "timeout", "service": "x"}, gw)
        assert result["logs"]["results"] == []
        assert "error" in result

    def test_unknown_tool(self):
        gw = _backends({})
        result = dispatch("not.a.tool", {}, gw)
        assert result["error"] == "unknown_tool"


# ---------------------------------------------------------------------------
# MCP protocol (FastAPI TestClient, no network)
# ---------------------------------------------------------------------------

class TestMcpProtocol:
    def setup_method(self):
        gw = _backends({
            "/api/v2/alerts": [_alert()],
            "/loki/api/v1/query_range": _loki_payload(),
            "/api/v1/query": _prom_vector(80.0),
            "/api/v1/query_range": _prom_range([80.0]),
        })
        self.client = TestClient(create_app(settings=gw.settings, backends=gw))

    def test_health(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["mode"] == "oss-validation"

    def test_initialize(self):
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        })
        assert resp.status_code == 200
        assert resp.headers.get("mcp-session-id")
        result = resp.json()["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert result["serverInfo"]["name"] == "sentinal-oss-validation"

    def test_initialized_notification_is_202(self):
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        assert resp.status_code == 202

    def test_tools_list_agentcore_names(self):
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        })
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert "SplunkTarget___search_oneshot" in names
        assert "MoogsoftTarget___get_incident_by_id" in names

    def test_tools_call_returns_json_in_text_content(self):
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "MoogsoftTarget___get_incident_by_id",
                "arguments": {"incident_id": "INC-OSS-001"},
            },
        })
        result = resp.json()["result"]
        text = result["content"][0]["text"]
        payload = json.loads(text)
        assert payload["incident"]["incident_id"] == "INC-OSS-001"
        assert result["isError"] is False

    def test_tools_call_logs(self):
        resp = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "SplunkTarget___search_oneshot",
                "arguments": {"query": "timeout payment-service", "service": "payment-service"},
            },
        })
        payload = json.loads(resp.json()["result"]["content"][0]["text"])
        assert payload["logs"]["count"] == 1

    def test_invoke_rest_helper(self):
        resp = self.client.post("/invoke", json={
            "toolName": "SysdigTarget___query_metrics",
            "toolInput": {"service": "payment-service", "metric": "response_time_ms"},
        })
        assert resp.status_code == 200
        assert "metrics" in resp.json()

    def test_auth_required_when_token_set(self):
        gw = _backends({"/api/v2/alerts": [_alert()]})
        gw.settings.gateway_token = "secret-token"
        client = TestClient(create_app(settings=gw.settings, backends=gw))
        denied = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
        })
        assert denied.status_code == 401
        ok = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert ok.status_code == 200

    def test_handle_rpc_unknown_method(self):
        rpc, _sid = handle_rpc(
            {"jsonrpc": "2.0", "id": 9, "method": "nope", "params": {}},
            dispatch=lambda n, a: {},
        )
        assert rpc is not None
        assert rpc["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Optional YAML playbook (does not touch default hardcoded path)
# ---------------------------------------------------------------------------

class TestOssPlaybook:
    def test_aliases_resolve_to_canonical_workers(self):
        result = load_yaml_playbooks(ROOT / "deploy" / "oss-validation" / "playbooks")
        assert "timeout" in result
        workers = [step["worker"] for step in result["timeout"]]
        assert "log_worker" in workers
        assert "metrics_worker" in workers
        assert all(w in CANONICAL_WORKERS for w in workers)

    def test_hardcoded_playbooks_untouched_without_flag(self, monkeypatch):
        monkeypatch.delenv("YAML_PLAYBOOKS_ENABLED", raising=False)
        import supervisor.tool_selector as ts
        ts._get_active_playbooks.__dict__.clear()
        from supervisor.tool_selector import INCIDENT_PLAYBOOKS, get_playbook
        steps = get_playbook("timeout")
        assert steps == INCIDENT_PLAYBOOKS["timeout"]


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

class TestOssDocs:
    def test_oss_validation_guide_exists(self):
        text = (ROOT / "docs" / "clone" / "OSS_VALIDATION.md").read_text()
        assert "GATEWAY_MODE=live" in text
        assert "INC-OSS-001" in text
        assert "MCP_GATEWAY_URL" in text

    def test_connect_doc_links_oss_guide(self):
        text = (ROOT / "docs" / "clone" / "CONNECT_YOUR_ENVIRONMENT.md").read_text()
        assert "OSS_VALIDATION.md" in text
        assert "INC-OSS-001" in text

    def test_readme_links_oss_guide(self):
        text = (ROOT / "README.md").read_text()
        assert "docs/clone/OSS_VALIDATION.md" in text

    def test_env_example_mentions_oss_path(self):
        text = (ROOT / ".env.example").read_text()
        assert "OSS_VALIDATION.md" in text
        assert "INC-OSS-001" in text
