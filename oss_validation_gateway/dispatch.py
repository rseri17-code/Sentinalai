"""Dispatch AgentCore-named tools onto OSS backends and shape the result."""

from __future__ import annotations

import logging
import time
from typing import Any

from oss_validation_gateway.backends import Backends
from oss_validation_gateway.names import parse_tool_name
from oss_validation_gateway.queries import (
    golden_signal_promql,
    metric_hint_to_promql,
    splunk_query_to_logql,
)
from oss_validation_gateway import shaping

logger = logging.getLogger("sentinalai.oss_validation_gateway")


def _service(params: dict[str, Any]) -> str:
    return str(params.get("service") or params.get("target") or params.get("deployment") or "").strip()


def _find_alert(alerts: list[dict[str, Any]], incident_id: str) -> dict[str, Any] | None:
    wanted = str(incident_id or "").strip()
    if not wanted:
        return None
    for alert in alerts:
        if shaping.incident_id_of(alert) == wanted:
            return alert
        fingerprint = str(alert.get("fingerprint") or "")
        if fingerprint and fingerprint == wanted:
            return alert
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        if str(labels.get("alertname") or "") == wanted:
            return alert
    return None


def _range_window(params: dict[str, Any]) -> tuple[str, str]:
    hours = int(params.get("time_window_hours") or params.get("window_hours") or 2)
    hours = max(1, min(hours, 48))
    end = int(time.time())
    start = end - hours * 3600
    return str(start), str(end)


def _loki_window_ns(params: dict[str, Any]) -> tuple[str, str]:
    start_s, end_s = _range_window(params)
    return str(int(start_s) * 1_000_000_000), str(int(end_s) * 1_000_000_000)


def _golden_values(backends: Backends, service: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for key in (
        "latency_p95",
        "latency_baseline_p95",
        "latency_p50",
        "latency_p99",
        "error_rate",
        "request_rate",
        "saturation",
    ):
        try:
            payload = backends.prometheus_query(golden_signal_promql(key, service or "payment-service"))
            values[key] = shaping.prometheus_scalar(payload, default=0.0)
        except Exception as exc:
            logger.warning("prometheus golden signal %s failed: %s", key, exc)
            values[key] = 0.0
    return values


def dispatch(tool_name: str, params: dict[str, Any] | None, backends: Backends) -> dict[str, Any]:
    """Return a stub-compatible dict for ``tool_name`` (gateway or dotted)."""
    arguments = params if isinstance(params, dict) else {}
    parsed = parse_tool_name(tool_name)
    if parsed is None:
        return {"error": "unknown_tool", "tool": tool_name, "source": "oss_validation"}
    server, operation = parsed
    try:
        return _dispatch_known(server, operation, arguments, backends)
    except Exception as exc:
        logger.warning("oss validation %s.%s failed: %s", server, operation, exc)
        return _fail_open(server, operation, exc)


def _fail_open(server: str, operation: str, exc: Exception) -> dict[str, Any]:
    base: dict[str, Any] = {
        "error": f"{server}_unreachable: {exc}",
        "source": "oss_validation",
        "server": server,
        "operation": operation,
    }
    if server in {"splunk"}:
        base["logs"] = {"results": [], "count": 0}
        if "change" in operation:
            base["changes"] = []
    elif server in {"sysdig", "signalfx", "dynatrace"}:
        if "event" in operation:
            base["events"] = []
        elif "problem" in operation:
            base["problems"] = []
        elif "golden" in operation or "signal" in operation or operation == "get_metrics":
            base["signals"] = {}
        else:
            base["metrics"] = {"metrics": [], "baseline": 0}
    elif server == "moogsoft":
        if operation == "get_incident_by_id":
            base["incident"] = None
        elif "alert" in operation:
            base["alerts"] = []
        else:
            base["incidents"] = []
    elif server == "kubernetes":
        base["success"] = False
    return base


def _dispatch_known(
    server: str,
    operation: str,
    params: dict[str, Any],
    backends: Backends,
) -> dict[str, Any]:
    service = _service(params)

    if server == "moogsoft":
        return _moogsoft(operation, params, backends)
    if server == "splunk":
        return _splunk(operation, params, service, backends)
    if server == "sysdig":
        return _sysdig(operation, params, service, backends)
    if server == "dynatrace":
        return _dynatrace(operation, params, service, backends)
    if server == "signalfx":
        return _signalfx(operation, params, service, backends)
    if server == "kubernetes":
        return _kubernetes(operation, params, service, backends)
    if server in {"servicenow", "github", "confluence"}:
        return shaping.shape_skip(server, operation)
    return shaping.shape_skip(server, operation)


def _moogsoft(operation: str, params: dict[str, Any], backends: Backends) -> dict[str, Any]:
    alerts = backends.alertmanager_alerts()
    if operation == "get_incident_by_id":
        incident_id = str(params.get("incident_id") or params.get("id") or "")
        match = _find_alert(alerts, incident_id)
        return shaping.shape_incident_wrapper(match, requested_id=incident_id)
    if operation in {"get_incidents", "get_critical_incidents"}:
        if operation == "get_critical_incidents":
            alerts = [a for a in alerts if shaping.shape_incident(a).get("severity", 5) <= 2]
        return shaping.shape_incidents(alerts)
    if operation == "get_alerts":
        return shaping.shape_alerts(alerts)
    if operation == "get_closed_incidents":
        closed = [a for a in alerts if not shaping.is_firing(a)]
        return shaping.shape_incidents(closed)
    return shaping.shape_skip("moogsoft", operation)


def _splunk(operation: str, params: dict[str, Any], service: str, backends: Backends) -> dict[str, Any]:
    if operation in {"search_oneshot", "search_export"}:
        query = str(params.get("query") or "")
        logql = splunk_query_to_logql(query, service)
        start_ns, end_ns = _loki_window_ns(params)
        payload = backends.loki_query_range(logql, start_ns=start_ns, end_ns=end_ns)
        shaped = shaping.shape_logs(payload, service=service)
        shaped["logql"] = logql
        return shaped
    if operation in {"get_change_data", "app_change_data"}:
        return shaping.shape_changes()
    if operation == "get_host_metrics":
        return _sysdig("query_metrics", params, service, backends)
    if operation == "get_health_status":
        return {"status": "ok", "source": "oss_validation"}
    if operation == "get_incident_data":
        return _moogsoft("get_incident_by_id", params, backends)
    return shaping.shape_skip("splunk", operation)


def _sysdig(operation: str, params: dict[str, Any], service: str, backends: Backends) -> dict[str, Any]:
    if operation in {"query_metrics", "get_host_metrics"}:
        metric = str(params.get("metric") or params.get("metric_hint") or "")
        promql = metric_hint_to_promql(metric, service or "payment-service")
        start, end = _range_window(params)
        try:
            payload = backends.prometheus_query_range(promql, start=start, end=end)
        except Exception:
            payload = backends.prometheus_query(promql)
        shaped = shaping.shape_metrics(payload, metric_name=metric or "request_rate", service=service)
        shaped["promql"] = promql
        return shaped
    if operation in {"golden_signals"}:
        values = _golden_values(backends, service or "payment-service")
        return shaping.shape_golden_signals(values, service=service)
    if operation in {"get_events", "get_kubernetes_events"}:
        alerts = backends.alertmanager_alerts()
        return shaping.shape_events(alerts)
    if operation == "discover_resources":
        return {"resources": [], "skipped": True, "source": "oss_validation"}
    if operation == "environment_status":
        return {"status": "ok", "source": "prometheus"}
    return shaping.shape_skip("sysdig", operation)


def _dynatrace(operation: str, params: dict[str, Any], service: str, backends: Backends) -> dict[str, Any]:
    if operation in {"get_metrics"}:
        values = _golden_values(backends, service or "payment-service")
        return shaping.shape_golden_signals(values, service=service)
    if operation == "get_problems":
        return shaping.shape_problems(backends.alertmanager_alerts())
    if operation == "get_events":
        return shaping.shape_events(backends.alertmanager_alerts())
    if operation == "get_entities":
        svc = service or "unknown"
        return {"entities": [{"name": svc, "type": "service"}], "source": "oss_validation"}
    return shaping.shape_skip("dynatrace", operation)


def _signalfx(operation: str, params: dict[str, Any], service: str, backends: Backends) -> dict[str, Any]:
    if operation == "query_signalfx_metrics":
        values = _golden_values(backends, service or "payment-service")
        return shaping.shape_golden_signals(values, service=service)
    if operation == "get_signalfx_active_incidents":
        return shaping.shape_incidents(backends.alertmanager_alerts())
    return shaping.shape_skip("signalfx", operation)


def _kubernetes(operation: str, params: dict[str, Any], service: str, backends: Backends) -> dict[str, Any]:
    namespace = str(params.get("namespace") or backends.settings.kubernetes_namespace or "default")
    name = service or str(params.get("deployment") or "unknown-service")

    if operation in {"rollback_deployment", "scale_service"} and not backends.settings.kube_mutations:
        key = "rollback" if "rollback" in operation else "scale"
        body = {
            "status": "skipped",
            "deployment": name,
            "namespace": namespace,
            "message": "oss_validation: Kubernetes mutations disabled (OSS_KUBE_MUTATIONS=false)",
        }
        if key == "scale":
            body["replicas"] = params.get("replicas", 2)
        return {key: body, "success": False, "skipped": True, "source": "oss_validation"}

    if not backends.kubernetes_configured():
        if operation == "get_deployment_status":
            return {
                "deployment": name,
                "namespace": namespace,
                "ready_replicas": 0,
                "desired_replicas": 0,
                "available": False,
                "error": "kubernetes_not_configured",
                "source": "oss_validation",
            }
        if operation == "get_pod_logs":
            return {"logs": [], "pod_count": 0, "error": "kubernetes_not_configured", "source": "oss_validation"}
        return {
            "success": False,
            "error": "kubernetes_not_configured",
            "service": name,
            "source": "oss_validation",
        }

    if operation == "get_deployment_status":
        path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        data = backends.kubernetes_get(path)
        status = data.get("status") if isinstance(data, dict) else {}
        spec = data.get("spec") if isinstance(data, dict) else {}
        ready = int((status or {}).get("readyReplicas") or 0)
        desired = int((spec or {}).get("replicas") or (status or {}).get("replicas") or 0)
        return {
            "deployment": name,
            "namespace": namespace,
            "ready_replicas": ready,
            "desired_replicas": desired,
            "available": ready > 0,
            "source": "kubernetes",
        }

    if operation == "get_pod_logs":
        path = f"/api/v1/namespaces/{namespace}/pods"
        listing = backends.kubernetes_get(path, params={"labelSelector": f"app={name}"})
        items = listing.get("items") if isinstance(listing, dict) else []
        logs: list[str] = []
        if isinstance(items, list):
            for pod in items[:3]:
                pod_name = (pod.get("metadata") or {}).get("name") if isinstance(pod, dict) else None
                if not pod_name:
                    continue
                raw = backends.kubernetes_get(
                    f"/api/v1/namespaces/{namespace}/pods/{pod_name}/log",
                    params={"tailLines": "50"},
                )
                if isinstance(raw, str) and raw.strip():
                    logs.extend(raw.splitlines()[-50:])
        return {"logs": logs, "pod_count": len(logs), "source": "kubernetes"}

    if operation == "rollback_deployment":
        return {
            "rollback": {
                "status": "unsupported",
                "deployment": name,
                "namespace": namespace,
                "message": "oss_validation: live rollback is not implemented; enable a real Kubernetes MCP target",
            },
            "success": False,
            "source": "oss_validation",
        }
    if operation == "scale_service":
        return {
            "scale": {
                "status": "unsupported",
                "deployment": name,
                "namespace": namespace,
                "replicas": params.get("replicas", 2),
            },
            "success": False,
            "source": "oss_validation",
        }
    return shaping.shape_skip("kubernetes", operation)
