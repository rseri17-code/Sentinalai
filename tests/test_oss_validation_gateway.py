"""Offline tests for the OSS validation MCP gateway.

No live network. HTTP backends are faked via an in-memory transport.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oss_validation_gateway.backends import LOKI_LINE_LIMIT, Backends, Settings
from oss_validation_gateway.dispatch import dispatch
from oss_validation_gateway.names import (
    DEFAULT_TARGETS,
    gateway_tool_name,
    parse_tool_name,
    target_for_server,
    tool_catalog,
)
from oss_validation_gateway.protocol import handle_rpc
from oss_validation_gateway.queries import (
    golden_signal_promql,
    metric_hint_to_promql,
    splunk_query_to_logql,
)
from oss_validation_gateway.server import create_app
from oss_validation_gateway.shaping import (
    incident_id_of,
    shape_alerts,
    shape_golden_signals,
    shape_incident,
    shape_incidents,
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

def _unquote_logql(body: str) -> str:
    return body.replace('\\"', '"').replace("\\\\", "\\")


def _line_matches(logql: str, line: str) -> bool:
    """True when every Loki ``|~`` filter in ``logql`` matches ``line``."""
    bodies = re.findall(r'\|~ "(.*?)"', logql)
    if not bodies:
        return False
    return all(re.search(_unquote_logql(body), line) is not None for body in bodies)


def _selected_services(query: str, candidates: list[str]) -> list[str]:
    """Services a label matcher would select.

    ``service="..."`` is equality. ``service=~"..."`` is a regex. A dot in
    an equality value must not select ``svcXv2``.
    """
    match = re.search(r'service=(~)?"((?:\\.|[^"])*)"', query)
    assert match is not None, query
    value = _unquote_logql(match.group(2))
    if match.group(1):
        return [candidate for candidate in candidates if re.fullmatch(value, candidate)]
    return [candidate for candidate in candidates if candidate == value]


class TestQueryTranslation:
    def test_timeout_hint_to_logql(self):
        logql = splunk_query_to_logql("timeout payment-service", "payment-service")
        assert 'service="payment-service"' in logql
        assert "|~" in logql
        assert "(?i)" in logql
        assert "timeout" in logql
        assert "timed out" in logql
        assert "deadline exceeded" in logql
        assert "|=" not in logql

    def test_timeout_hint_is_case_insensitive(self):
        assert splunk_query_to_logql("TIMEOUT", "payment-service") == splunk_query_to_logql(
            "timeout", "payment-service"
        )

    def test_service_token_dropped_case_insensitively(self):
        logql = splunk_query_to_logql("TIMEOUT Payment-Service", "payment-service")
        assert "Payment-Service" not in logql
        assert logql.count("|~") == 1

    def test_pool_synonyms(self):
        logql = splunk_query_to_logql("pool", "checkout")
        assert "connection pool" in logql
        assert "pool exhausted" in logql
        assert "pool\\\\.exhausted" in logql

    def test_error_covers_exception(self):
        logql = splunk_query_to_logql("ERROR", "api")
        assert "exception" in logql
        assert "(?i)" in logql

    def test_synonym_token_uses_the_whole_set(self):
        logql = splunk_query_to_logql("exception", "api")
        assert "error" in logql
        assert "exception" in logql
        assert logql.count("|~") == 1

    def test_multi_keyword_hints_are_all_kept(self):
        logql = splunk_query_to_logql("timeout error", "api")
        assert logql.count("|~") == 1
        assert "(?:timeout|timed out|deadline exceeded).*(?:error|exception)" in logql
        assert "(?:error|exception).*(?:timeout|timed out|deadline exceeded)" in logql

    def test_unknown_keywords_are_all_kept(self):
        logql = splunk_query_to_logql("cascade restart", "api")
        assert logql.count("|~") == 1
        assert '|~ "(?i)(?:cascade.*restart|restart.*cascade)"' in logql

    def test_or_terms_are_alternatives(self):
        logql = splunk_query_to_logql("latency OR slow", "api")
        assert logql.count("|~") == 1
        assert '|~ "(?i)(?:latency|slow)"' in logql
        assert splunk_query_to_logql("latency or slow", "api") == logql

    def test_two_word_hint_is_order_free(self):
        logql = splunk_query_to_logql("connection refused", "api")
        assert logql.count("|~") == 1
        assert '|~ "(?i)(?:connection.*refused|refused.*connection)"' in logql
        assert _line_matches(logql, "connection refused by upstream")
        assert _line_matches(logql, "refused connection from client")
        assert not _line_matches(logql, "connection accepted")

    def test_three_word_hint_matches_every_order(self):
        logql = splunk_query_to_logql("alpha beta gamma", "api")
        assert (
            '|~ "(?i)(?:alpha.*beta.*gamma|alpha.*gamma.*beta|'
            "beta.*alpha.*gamma|beta.*gamma.*alpha|"
            'gamma.*alpha.*beta|gamma.*beta.*alpha)"'
        ) in logql
        for order in itertools.permutations(("alpha", "beta", "gamma")):
            assert _line_matches(logql, " ".join(order))
        assert not _line_matches(logql, "alpha beta")

    def test_four_word_hint_raises(self):
        with pytest.raises(ValueError, match="4 words"):
            splunk_query_to_logql("alpha beta gamma delta", "api")
        with pytest.raises(ValueError, match="at most 3"):
            splunk_query_to_logql("alpha beta gamma delta OR dns", "api")

    def test_hint_filter_is_deterministic(self):
        hint = "connection refused OR dns"
        first = splunk_query_to_logql(hint, "api")
        assert first == splunk_query_to_logql(hint, "api")
        three = [splunk_query_to_logql("gamma beta alpha", "api") for _ in range(20)]
        assert len(set(three)) == 1

    def test_mixed_or_query(self):
        logql = splunk_query_to_logql("a b OR c", "api")
        assert logql.count("|~") == 1
        assert '|~ "(?i)(?:(?:a.*b|b.*a)|c)"' in logql

    def test_and_and_not_are_not_hints(self):
        logql = splunk_query_to_logql("a AND b", "api")
        assert "AND" not in logql
        assert '|~ "(?i)(?:a.*b|b.*a)"' in logql

    @pytest.mark.parametrize(
        ("hint", "literal", "negatives"),
        [
            ("a+b", "a+b", ["ab", "aaab", "aXb"]),
            ("x|y", "x|y", ["xy", "x", "y"]),
            ("a(b", "a(b", ["ab", "aXb"]),
        ],
    )
    def test_metacharacters_match_literally(self, hint, literal, negatives):
        logql = splunk_query_to_logql(hint, "api")
        assert _line_matches(logql, literal)
        assert _line_matches(logql, literal.upper())
        for line in negatives:
            assert not _line_matches(logql, line), line

    def test_dotted_service_matches_only_itself(self):
        logql = splunk_query_to_logql("timeout", "svc.v2")
        promql = metric_hint_to_promql("request_rate", "svc.v2")
        golden = golden_signal_promql("latency_p95", "svc.v2")
        candidates = ["svc.v2", "svcXv2", "svc.v2.extra"]
        assert _selected_services(logql, candidates) == ["svc.v2"]
        assert _selected_services(promql, candidates) == ["svc.v2"]
        assert _selected_services(golden, candidates) == ["svc.v2"]
        # The same name, used as a line filter, is escaped. An unescaped
        # dot would match svcXv2.
        filtered = splunk_query_to_logql("svc.v2", "api")
        assert _line_matches(filtered, "down svc.v2 now")
        assert not _line_matches(filtered, "down svcXv2 now")

    def test_empty_query_selects_service(self):
        logql = splunk_query_to_logql("", "api-gateway")
        assert logql == '{service="api-gateway"}'

    def test_empty_service_raises(self):
        with pytest.raises(ValueError, match="empty"):
            splunk_query_to_logql("timeout", "")
        with pytest.raises(ValueError, match="empty"):
            splunk_query_to_logql("timeout", None)
        with pytest.raises(ValueError, match="empty"):
            splunk_query_to_logql("timeout", "   ")

    def test_invalid_service_raises(self):
        with pytest.raises(ValueError, match="invalid"):
            splunk_query_to_logql("timeout", 'pay"ment')
        with pytest.raises(ValueError, match="invalid"):
            splunk_query_to_logql("timeout", "pay ment")

    def test_metric_hint_promql(self):
        assert "demo_latency_p95_ms" in metric_hint_to_promql("response_time_ms", "payment-service")
        assert "process_resident_memory_bytes" in metric_hint_to_promql(
            "memory_usage_bytes", "payment-service"
        )

    def test_metric_hint_is_case_insensitive(self):
        assert metric_hint_to_promql("Response_Time_MS", "payment-service") == metric_hint_to_promql(
            "response_time_ms", "payment-service"
        )

    def test_metric_and_golden_reject_empty_service(self):
        with pytest.raises(ValueError, match="empty"):
            metric_hint_to_promql("request_rate", "")
        with pytest.raises(ValueError, match="empty"):
            metric_hint_to_promql("request_rate", None)
        with pytest.raises(ValueError, match="empty"):
            golden_signal_promql("latency_p95", "")
        with pytest.raises(ValueError, match="empty"):
            golden_signal_promql("latency_p95", "   ")

    def test_unknown_metric_hint_raises(self):
        with pytest.raises(KeyError):
            metric_hint_to_promql("not_a_real_metric", "payment-service")

    def test_empty_metric_hint_raises(self):
        with pytest.raises(KeyError):
            metric_hint_to_promql("", "payment-service")
        with pytest.raises(KeyError):
            metric_hint_to_promql(None, "payment-service")
        with pytest.raises(KeyError):
            metric_hint_to_promql("   ", "payment-service")

    def test_db_connection_pool_active_errors_on_this_seed(self):
        seed = Path("deploy/oss-validation/seed/seed.py").read_text(encoding="utf-8")
        assert "db_connection_pool_active" not in seed
        assert "connection_pool" not in seed
        with pytest.raises(KeyError):
            metric_hint_to_promql("db_connection_pool_active", "payment-service")

    def test_not_is_rejected(self):
        with pytest.raises(ValueError, match="not"):
            splunk_query_to_logql("error not timeout", "api")
        with pytest.raises(ValueError, match="not"):
            splunk_query_to_logql("NOT timeout", "api")
        logql = splunk_query_to_logql("notification", "api")
        assert "notification" in logql


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

    def test_capped_loki_query_reports_truncation(self):
        values = []
        start = 1_700_000_000
        for i in range(LOKI_LINE_LIMIT):
            values.append([str((start + i) * 1_000_000_000), f"line {i}"])
        payload = {
            "data": {
                "result": [{
                    "stream": {"service": "api"},
                    "values": values,
                }],
            },
        }
        shaped = shape_logs(
            payload,
            limit=LOKI_LINE_LIMIT,
            window_start="2023-11-14T22:13:20Z",
            window_end="2023-11-15T00:13:20Z",
        )
        assert shaped["logs"]["count"] == LOKI_LINE_LIMIT
        assert shaped["truncated"] is True
        assert shaped["limit"] == LOKI_LINE_LIMIT
        assert shaped["oldest_ts"] == "2023-11-14T22:13:20Z"
        assert shaped["newest_ts"] == "2023-11-14T22:14:09Z"
        assert shaped["window_start"] == "2023-11-14T22:13:20Z"
        assert shaped["window_end"] == "2023-11-15T00:13:20Z"

    def test_uncapped_loki_query_is_not_truncated(self):
        shaped = shape_logs(
            _loki_payload(),
            limit=LOKI_LINE_LIMIT,
            window_start="2024-01-15T10:40:11Z",
            window_end="2024-01-15T12:40:11Z",
        )
        assert shaped["logs"]["count"] == 1
        assert shaped["truncated"] is False
        assert shaped["oldest_ts"] == "2024-01-15T12:40:11Z"
        assert shaped["newest_ts"] == "2024-01-15T12:40:11Z"

    def test_backend_truncated_flag_is_reported(self):
        payload = _loki_payload()
        payload["data"]["truncated"] = True
        shaped = shape_logs(payload, limit=LOKI_LINE_LIMIT)
        assert shaped["logs"]["count"] == 1
        assert shaped["truncated"] is True

    def test_reported_counts_match_returned_records(self):
        logs = shape_logs(_loki_payload())
        assert logs["logs"]["count"] == len(logs["logs"]["results"])
        assert "result_count" not in logs
        assert logs["truncated"] is False
        assert logs["limit"] is None
        incidents = shape_incidents([_alert(), _alert("INC-OSS-002")])
        assert incidents["count"] == len(incidents["incidents"])
        assert incidents["truncated"] is False
        assert incidents["oldest_ts"]
        alerts = shape_alerts([_alert()])
        assert alerts["count"] == len(alerts["alerts"])
        assert alerts["truncated"] is False
        assert alerts["oldest_ts"] == alerts["newest_ts"]

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
        assert result["logs"]["count"] == len(result["logs"]["results"])
        assert "timeout" in result["logs"]["results"][0]["message"]
        assert "(?i)" in result["logql"]
        assert "timeout" in result["logql"]
        assert result["truncated"] is False
        assert result["limit"] == LOKI_LINE_LIMIT
        assert result["window_start"]
        assert result["window_end"]
        assert result["oldest_ts"] == "2024-01-15T12:40:11Z"
        assert result["newest_ts"] == result["oldest_ts"]
        params = gw.transport.calls[0][2]
        assert params is not None
        assert params["limit"] == str(LOKI_LINE_LIMIT)
        assert params["direction"] == "backward"

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
        assert result["truncated"] is False
        assert result["limit"] is None
        assert result["step"] == "30s"
        assert result["range"]
        assert result["oldest_ts"]
        assert result["newest_ts"]
        assert result["window_start"]
        assert result["window_end"]

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

    def test_kubernetes_pod_logs_report_caps(self):
        gw = _backends({})
        result = dispatch("kubernetes.get_pod_logs", {"service": "payment-service"}, gw)
        assert result["error"] == "kubernetes_not_configured"
        assert result["pods_limit"] == 3
        assert result["lines_per_pod_limit"] == 50
        assert result["pods_total"] is None
        assert result["limit"] == 150
        assert result["truncated"] is False
        assert result["oldest_ts"] is None
        assert result["newest_ts"] is None

    def test_kubernetes_status_without_cluster(self):
        gw = _backends({})
        result = dispatch(
            "kubernetes.get_deployment_status",
            {"service": "payment-service"},
            gw,
        )
        assert result["error"] == "kubernetes_not_configured"
        assert result["available"] is False

    def test_invalid_service_is_not_rewritten(self):
        gw = _backends({"/loki/api/v1/query_range": _loki_payload()})
        result = dispatch(
            "splunk.search_oneshot",
            {"query": "timeout", "service": 'pay"ment'},
            gw,
        )
        assert gw.transport.calls == []
        assert "invalid" in result["error"]
        assert result["logs"]["results"] == []

    def test_unknown_metric_returns_error_payload(self):
        gw = _backends({"/api/v1/query_range": _prom_range([1.0]), "/api/v1/query": _prom_vector(1.0)})
        result = dispatch(
            "SysdigTarget___query_metrics",
            {"service": "payment-service", "metric": "not_a_real_metric"},
            gw,
        )
        assert gw.transport.calls == []
        assert result["error"] == "unknown_metric: not_a_real_metric"
        assert result["metrics"]["metrics"] == []
        assert "promql" not in result
        assert "demo_request_rate" not in str(result)

    def test_empty_metric_returns_error_payload(self):
        gw = _backends({"/api/v1/query_range": _prom_range([1.0])})
        result = dispatch(
            "sysdig.query_metrics",
            {"service": "payment-service", "metric": ""},
            gw,
        )
        assert gw.transport.calls == []
        assert result["error"] == "unknown_metric: "
        missing = dispatch("sysdig.query_metrics", {"service": "payment-service"}, gw)
        assert gw.transport.calls == []
        assert missing["error"] == "unknown_metric: "
        blank = dispatch(
            "sysdig.query_metrics",
            {"service": "payment-service", "metric_hint": "   "},
            gw,
        )
        assert gw.transport.calls == []
        assert blank["error"] == "unknown_metric:    "

    def test_db_connection_pool_active_tool_errors(self):
        gw = _backends({"/api/v1/query_range": _prom_range([1.0]), "/api/v1/query": _prom_vector(1.0)})
        result = dispatch(
            "SysdigTarget___query_metrics",
            {"service": "payment-service", "metric": "db_connection_pool_active"},
            gw,
        )
        assert gw.transport.calls == []
        assert result["error"] == "unknown_metric: db_connection_pool_active"
        assert result["metrics"]["metrics"] == []
        assert "demo_request_rate" not in str(result)

    def test_metrics_missing_service_is_not_defaulted(self):
        gw = _backends({"/api/v1/query_range": _prom_range([1.0]), "/api/v1/query": _prom_vector(1.0)})
        result = dispatch("SysdigTarget___query_metrics", {"metric": "response_time_ms"}, gw)
        assert gw.transport.calls == []
        assert "empty" in result["error"]
        assert "payment-service" not in result["error"]

    def _golden_by_query(self, saturation_payload: Any):
        def route(method: str, url: str, params: dict[str, Any] | None, body: Any) -> Any:
            query = str((params or {}).get("query") or "")
            if "demo_saturation_pct" in query:
                if isinstance(saturation_payload, Exception):
                    raise saturation_payload
                return saturation_payload
            return _prom_vector(12.0)

        return _backends({"/api/v1/query": route})

    def test_saturation_error_is_unavailable_not_zero(self):
        gw = self._golden_by_query(ConnectionError("prometheus down"))
        result = dispatch("sysdig.golden_signals", {"service": "payment-service"}, gw)
        golden = result["signals"]["golden_signals"]
        assert "saturation" not in golden
        assert "saturation_pct" not in result["metrics"]
        assert {"signal": "saturation", "reason": "error"} in result["unavailable_signals"]
        assert golden["latency"]["p95"] == 12.0

    def test_saturation_empty_is_unavailable_not_zero(self):
        empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
        gw = self._golden_by_query(empty)
        result = dispatch("sysdig.golden_signals", {"service": "payment-service"}, gw)
        golden = result["signals"]["golden_signals"]
        assert "saturation" not in golden
        assert "saturation_pct" not in result["metrics"]
        assert {"signal": "saturation", "reason": "empty"} in result["unavailable_signals"]
        assert golden["latency"]["p95"] == 12.0

    def test_saturation_measured_zero_stays_zero(self):
        gw = self._golden_by_query(_prom_vector(0.0))
        result = dispatch("sysdig.golden_signals", {"service": "payment-service"}, gw)
        golden = result["signals"]["golden_signals"]
        assert golden["saturation"]["pct"] == 0
        assert result["metrics"]["saturation_pct"] == 0
        assert result.get("unavailable_signals", []) == []

    def test_golden_signals_missing_service_is_not_defaulted(self):
        gw = _backends({"/api/v1/query": _prom_vector(1.0)})
        result = dispatch("DynatraceTarget___get_metrics", {}, gw)
        assert gw.transport.calls == []
        assert "empty" in result["error"]
        result = dispatch("sysdig.golden_signals", {"service": "   "}, gw)
        assert gw.transport.calls == []
        assert "empty" in result["error"]
        result = dispatch("signalfx.query_signalfx_metrics", {}, gw)
        assert gw.transport.calls == []
        assert "empty" in result["error"]

    def test_empty_service_is_not_rewritten(self):
        gw = _backends({"/loki/api/v1/query_range": _loki_payload()})
        result = dispatch("splunk.search_oneshot", {"query": "timeout", "service": ""}, gw)
        assert gw.transport.calls == []
        assert "empty" in result["error"]
        assert result["logs"]["count"] == 0

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
        assert "search_pool_logs" in [step.get("label") for step in steps]


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
