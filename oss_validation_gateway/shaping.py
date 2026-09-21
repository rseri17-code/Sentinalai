"""Normalize OSS backend payloads into shapes workers/stubs already tolerate.

Contracts (from ``workers/mcp_client.py`` stubs + ``supervisor/agent.py`` extractors):

- ops:    ``{"incident": {incident_id, summary, affected_service, severity, status, ...}}``
- logs:   ``{"logs": {"results": [...], "count": N}}``  with ``message`` / ``_raw`` / ``_time``
- metrics:``{"metrics": {"metrics": [...], "baseline": 0}}``
- signals:``{"signals": {"golden_signals": {"latency": {p95, baseline_p95}, "errors": {rate}}}}``
- events: ``{"events": [...]}``
- changes:``{"changes": []}``
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from oss_validation_gateway.names import parse_tool_name


def _iso(ts: Any) -> str:
    if ts is None or ts == "":
        return ""
    if isinstance(ts, str):
        return ts
    try:
        value = float(ts)
        if value > 1e12:
            value = value / 1000.0
        if value > 1e12:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _ns_to_iso(ns: str | int) -> str:
    try:
        seconds = int(ns) / 1_000_000_000
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return ""


def _labels(alert: dict[str, Any]) -> dict[str, str]:
    raw = alert.get("labels") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _annotations(alert: dict[str, Any]) -> dict[str, str]:
    raw = alert.get("annotations") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _severity(alert: dict[str, Any]) -> int:
    labels = _labels(alert)
    raw = (labels.get("severity") or labels.get("priority") or "").strip().lower()
    mapping = {
        "critical": 1,
        "page": 1,
        "high": 2,
        "major": 2,
        "warning": 3,
        "medium": 3,
        "minor": 4,
        "low": 4,
        "info": 5,
        "none": 5,
    }
    if raw in mapping:
        return mapping[raw]
    try:
        return max(1, min(5, int(raw)))
    except (TypeError, ValueError):
        return 1 if is_firing(alert) else 3


def is_firing(alert: dict[str, Any]) -> bool:
    status = alert.get("status")
    if isinstance(status, dict):
        state = str(status.get("state") or "").lower()
        return state in {"active", "firing"}
    if isinstance(status, str):
        return status.lower() in {"active", "firing", "open"}
    return True


def incident_id_of(alert: dict[str, Any]) -> str:
    labels = _labels(alert)
    annotations = _annotations(alert)
    return (
        labels.get("incident_id")
        or annotations.get("incident_id")
        or labels.get("alertname")
        or str(alert.get("fingerprint") or "")
    )


def shape_incident(alert: dict[str, Any]) -> dict[str, Any]:
    labels = _labels(alert)
    annotations = _annotations(alert)
    service = labels.get("service") or labels.get("job") or "unknown"
    summary = annotations.get("summary") or labels.get("alertname") or "alert"
    description = annotations.get("description") or summary
    incident_id = incident_id_of(alert)
    status = "open" if is_firing(alert) else "closed"
    return {
        "incident_id": incident_id,
        "summary": summary,
        "affected_service": service,
        "severity": _severity(alert),
        "status": status,
        "source": "alertmanager",
        "created_at": _iso(alert.get("startsAt") or alert.get("starts_at")),
        "updated_at": _iso(alert.get("updatedAt") or alert.get("startsAt")),
        "description": description,
        "tags": ["oss-validation", labels.get("alertname", "")],
        "fingerprint": str(alert.get("fingerprint") or ""),
    }


def shape_incidents(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    incidents = [shape_incident(a) for a in alerts]
    return {"incidents": incidents, "count": len(incidents), "source": "alertmanager"}


def shape_incident_wrapper(alert: dict[str, Any] | None, requested_id: str = "") -> dict[str, Any]:
    if alert is None:
        return {
            "incident": None,
            "error": "incident_not_found",
            "incident_id": requested_id,
            "source": "alertmanager",
        }
    return {"incident": shape_incident(alert), "source": "alertmanager"}


def shape_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for alert in alerts:
        labels = _labels(alert)
        annotations = _annotations(alert)
        items.append({
            "alert_id": str(alert.get("fingerprint") or incident_id_of(alert)),
            "severity": _severity(alert),
            "service": labels.get("service", "unknown"),
            "status": "open" if is_firing(alert) else "closed",
            "summary": annotations.get("summary") or labels.get("alertname", ""),
            "fingerprint": str(alert.get("fingerprint") or ""),
        })
    return {"alerts": items, "count": len(items), "source": "alertmanager"}


def shape_problems(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    problems = []
    for alert in alerts:
        incident = shape_incident(alert)
        problems.append({
            "problemId": incident["incident_id"],
            "displayName": incident["summary"],
            "severityLevel": "ERROR" if incident["severity"] <= 2 else "PERFORMANCE",
            "status": "OPEN" if incident["status"] == "open" else "CLOSED",
            "startTime": incident.get("created_at", ""),
            "affectedEntities": [{"name": incident["affected_service"]}],
        })
    return {"problems": problems, "source": "alertmanager"}


def shape_logs(loki_payload: dict[str, Any], service: str = "") -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    data = loki_payload.get("data") if isinstance(loki_payload, dict) else None
    streams = []
    if isinstance(data, dict):
        raw = data.get("result") or []
        if isinstance(raw, list):
            streams = raw
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream") if isinstance(stream.get("stream"), dict) else {}
        values = stream.get("values") or []
        if not isinstance(values, list):
            continue
        svc = str(labels.get("service") or service or "unknown")
        for pair in values:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            ts_ns, line = pair[0], str(pair[1])
            level = "ERROR" if "error" in line.lower() or "timeout" in line.lower() else "INFO"
            if "error" in str(labels.get("level", "")).lower():
                level = "ERROR"
            results.append({
                "_time": _ns_to_iso(ts_ns),
                "timestamp": _ns_to_iso(ts_ns),
                "host": str(labels.get("pod") or labels.get("host") or f"{svc}-pod"),
                "source": "loki",
                "sourcetype": "loki:log",
                "index": "oss-validation",
                "_raw": line,
                "level": level,
                "service": svc,
                "message": line,
                "downstream": _downstream_from_line(line),
            })
    return {"logs": {"results": results, "count": len(results)}, "source": "loki"}


def _downstream_from_line(line: str) -> str:
    # Mirror supervisor.agent._find_downstream_service: "timeout ...: svc"
    lowered = line.lower()
    if "timeout" not in lowered:
        return ""
    marker = ":"
    idx = lowered.rfind(marker)
    if idx == -1:
        return ""
    token = line[idx + 1:].strip().split()[0] if line[idx + 1:].strip() else ""
    return token.rstrip(".,;")


def _prom_samples(payload: dict[str, Any]) -> list[tuple[float, float, dict[str, str]]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    result = data.get("result") or []
    if not isinstance(result, list):
        return []
    samples: list[tuple[float, float, dict[str, str]]] = []
    for series in result:
        if not isinstance(series, dict):
            continue
        metric = series.get("metric") if isinstance(series.get("metric"), dict) else {}
        labels = {str(k): str(v) for k, v in metric.items()}
        if "value" in series and isinstance(series["value"], (list, tuple)) and len(series["value"]) >= 2:
            ts, val = series["value"][0], series["value"][1]
            try:
                samples.append((float(ts), float(val), labels))
            except (TypeError, ValueError):
                continue
        values = series.get("values") or []
        if isinstance(values, list):
            for pair in values:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                try:
                    samples.append((float(pair[0]), float(pair[1]), labels))
                except (TypeError, ValueError):
                    continue
    return samples


def _scalar(payload: dict[str, Any], default: float = 0.0) -> float:
    samples = _prom_samples(payload)
    if not samples:
        return default
    return samples[-1][1]


def shape_metrics(payload: dict[str, Any], metric_name: str = "", service: str = "") -> dict[str, Any]:
    points = []
    for ts, value, labels in _prom_samples(payload):
        points.append({
            "timestamp": _iso(ts),
            "value": value,
            "metric": metric_name or labels.get("__name__", "metric"),
            "service": labels.get("service") or service or "unknown",
        })
    baseline = points[0]["value"] if points else 0
    return {
        "metrics": {
            "metrics": points,
            "baseline": baseline,
            "pattern": "none",
        },
        "source": "prometheus",
    }


def shape_golden_signals(
    values: dict[str, float],
    service: str = "",
) -> dict[str, Any]:
    p95 = float(values.get("latency_p95") or 0)
    baseline = float(values.get("latency_baseline_p95") or 0)
    return {
        "signals": {
            "golden_signals": {
                "latency": {
                    "p95": p95,
                    "baseline_p95": baseline,
                    "p50": float(values.get("latency_p50") or 0),
                    "p99": float(values.get("latency_p99") or 0),
                },
                "errors": {"rate": float(values.get("error_rate") or 0)},
                "traffic": {"rps": float(values.get("request_rate") or 0)},
                "saturation": {"pct": float(values.get("saturation") or 0)},
            },
            "service": service,
        },
        "metrics": {
            "service": service,
            "error_rate": float(values.get("error_rate") or 0),
            "latency_p95": p95,
            "p95_ms": p95,
            "latency_p50_ms": float(values.get("latency_p50") or 0),
            "latency_p99_ms": float(values.get("latency_p99") or 0),
            "request_rate": float(values.get("request_rate") or 0),
            "rps": float(values.get("request_rate") or 0),
            "saturation_pct": float(values.get("saturation") or 0),
        },
        "source": "prometheus",
    }


def shape_events(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    events = []
    for alert in alerts:
        incident = shape_incident(alert)
        events.append({
            "timestamp": incident.get("created_at", ""),
            "message": incident.get("summary", ""),
            "reason": _labels(alert).get("alertname", "alert"),
            "service": incident.get("affected_service", ""),
            "severity": incident.get("severity", 3),
        })
    return {"events": events, "source": "alertmanager"}


def shape_changes() -> dict[str, Any]:
    return {
        "changes": [],
        "change_data": [],
        "source": "oss_validation",
        "skipped": True,
        "detail": "No change feed in the OSS validation shim (Loki/Prometheus/Alertmanager).",
    }


def shape_skip(server: str, operation: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Honest empty payload for backends this shim does not front."""
    payload: dict[str, Any] = {
        "skipped": True,
        "source": "oss_validation",
        "server": server,
        "operation": operation,
        "detail": f"{server}.{operation} is not mapped to an OSS backend.",
    }
    if server == "servicenow":
        if "incident" in operation:
            payload["incidents"] = []
        elif "change" in operation:
            payload["change_records"] = []
        elif "known" in operation or "error" in operation:
            payload["known_errors"] = []
        else:
            payload["ci"] = {}
    elif server == "confluence":
        if "runbook" in operation:
            payload["runbooks"] = []
        elif "postmortem" in operation:
            payload["postmortems"] = []
        else:
            payload["page"] = {}
    elif server == "github":
        if "deployment" in operation:
            payload["deployments"] = []
        elif "create" in operation and ("pr" in operation or "pull" in operation):
            payload.update({
                "pr": None,
                "created": False,
                "stub": True,
                "error": "github_not_in_oss_validation",
                "detail": (
                    "No PR was created — the OSS validation gateway does not "
                    "front GitHub. Configure a real GitHub MCP target for remediation."
                ),
            })
        elif "pr" in operation or "pull" in operation:
            payload.update({"pr": None, "error": "github_not_in_oss_validation"})
        elif "commit" in operation or "diff" in operation:
            payload["commit"] = {}
        elif "workflow" in operation:
            payload["workflow_runs"] = []
        elif "review" in operation:
            payload["ok"] = False
    elif server == "moogsoft" and "historical" in operation:
        payload["analysis"] = {}
    if extra:
        payload.update(extra)
    return payload


def empty_stub_for(tool_name: str) -> dict[str, Any]:
    parsed = parse_tool_name(tool_name)
    if parsed is None:
        return {"error": "unknown_tool", "tool": tool_name, "source": "oss_validation"}
    server, operation = parsed
    return shape_skip(server, operation)


def prometheus_scalar(payload: dict[str, Any], default: float = 0.0) -> float:
    return _scalar(payload, default=default)
