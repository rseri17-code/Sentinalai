"""Telemetry rendering — EFIC abstract telemetry → production-shaped MCP responses.

The EFIC corpus stores telemetry as opaque evidence keyed by an abstract MCP name
(``{"kubernetes": {...}, "sysdig": {...}, "certificates": {...}}``). The real
SentinelAI engine does not read that blob — it *queries* a fixed set of MCP
servers (splunk, dynatrace, sysdig, servicenow, github, confluence) through the
``McpGateway`` boundary. This module renders each scenario's telemetry into the
exact response schemas those servers return, so the unmodified engine consumes it.

Canonical response shapes are pinned by ``workers/mcp_client.py`` stub dispatch
and the ``_extract_*`` readers in ``supervisor/agent.py``; workers pass the
gateway's output through unchanged.

Honesty boundaries (recorded in :func:`render` provenance, surfaced in the report):

* The engine has **native collection channels** only for splunk / dynatrace /
  sysdig / servicenow (+ kubernetes events via sysdig, + github/confluence
  proof-gated). EFIC sources without a native channel (certificates, route53_dns,
  identity, aws_cloudwatch, autosys, cmdb, network, application, thousandeyes)
  are **folded into Splunk log lines** — which is how such failures actually
  surface to a log-first investigation (EFIC already authors most of these as
  splunk errors). The fold carries the observable *symptom*, never the hidden
  root-cause sentence (which lives in ``task.ground_truth``, never in telemetry).
* Golden-signal numbers are synthesized generically (an anomalous-but-not-
  scenario-tuned block), because EFIC does not encode per-metric values. This is
  a documented limitation, not a shortcut: the specifics the engine reasons over
  come from log/event/change text, not from the synthesized magnitudes.

Everything here is deterministic: fixed timestamps, sorted iteration, no clock,
no randomness. Input is the PUBLIC part of the task only (incident + telemetry);
``ground_truth``/``traps``/``efic`` never enter this module.
"""
from __future__ import annotations

from typing import Any, Mapping

# Deterministic incident anchor. The engine derives all change-lookback windows
# from incident timestamps (never wall-clock), so a fixed anchor => deterministic
# replay. Change records are stamped just inside the lookback window.
BASE_TS = "2024-01-01T00:00:00Z"
CHANGE_TS = "2023-12-31T23:00:00Z"        # 1h before the incident
ANOMALY_TS = "2023-12-31T23:55:00Z"

# EFIC source -> the engine collection channel(s) that natively carry it.
NATIVE_CHANNELS = {
    "splunk": "splunk.logs",
    "dynatrace": "dynatrace.golden_signals",
    "sysdig": "sysdig.metrics_events",
    "database": "sysdig.metrics",          # DB pool metrics are exposed via Sysdig
    "kubernetes": "sysdig.events",         # k8s events surface through Sysdig
    "servicenow": "servicenow.changes",
    "github": "github.deployments",
}
# EFIC sources the engine cannot query directly — folded into Splunk logs.
FOLDED_SOURCES = (
    "certificates", "route53_dns", "identity", "aws_cloudwatch", "autosys",
    "cmdb", "network", "application", "thousandeyes", "moogsoft", "signalfx",
)


def _flatten(value: Any) -> str:
    """Deterministically serialize a telemetry value into a compact symptom
    string (sorted keys; no clock, no randomness). Carries the observable signal
    a log line would show — never the hidden root-cause sentence."""
    if isinstance(value, Mapping):
        return " ".join(f"{k}={_flatten(value[k])}" for k in sorted(value))
    if isinstance(value, (list, tuple)):
        return "; ".join(_flatten(v) for v in value)
    return str(value)


def _log_line(source: str, message: str) -> dict[str, Any]:
    return {
        "_time": BASE_TS,
        "timestamp": BASE_TS,
        "level": "ERROR",
        "source": source,
        "message": message,
    }


def _splunk_logs(incident: Mapping[str, Any],
                 telemetry: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Render Splunk search results: native splunk errors + folded symptom lines.

    Returns (response, folded_sources)."""
    service = str(incident.get("service", "unknown"))
    results: list[dict[str, Any]] = []
    folded: list[str] = []

    # Native splunk errors (EFIC authors these as the observable log symptom).
    splunk = telemetry.get("splunk")
    if isinstance(splunk, Mapping):
        errs = splunk.get("errors")
        if isinstance(errs, (list, tuple)):
            for e in errs:
                results.append({**_log_line("splunk", str(e)), "service": service})
        else:
            for k in sorted(splunk):
                if k == "changes":
                    continue
                results.append({**_log_line("splunk", f"{k}: {_flatten(splunk[k])}"),
                                "service": service})

    # Fold non-native sources into log lines (deterministic order).
    for src in FOLDED_SOURCES:
        payload = telemetry.get(src)
        if not payload:
            continue
        results.append({**_log_line(src, f"[{src}] {_flatten(payload)}"),
                        "service": service})
        folded.append(src)

    return {"logs": {"results": results, "count": len(results),
                     "first_occurrence": BASE_TS}}, folded


def _change_records(telemetry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract change records from any source that describes a change."""
    records: list[dict[str, Any]] = []
    seq = 0
    for src in sorted(telemetry):
        payload = telemetry[src]
        if not isinstance(payload, Mapping):
            continue
        for key in ("change", "policy_change", "config_change", "config_version"):
            if key not in payload:
                continue
            desc = _flatten(payload[key])
            if desc.strip().lower() in ("none in window", "none"):
                continue
            seq += 1
            num = desc.split()[0] if desc.split() and desc.split()[0].upper().startswith(
                "CHG") else f"CHG-EFIC-{seq}"
            records.append({
                "number": num,
                "type": "change",
                "short_description": desc,
                "state": "implemented",
                "start_date": CHANGE_TS,
                "end_date": CHANGE_TS,
                "source": src,
            })
    return records


def _golden_signals(telemetry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Synthesize an anomalous golden-signals block when the scenario exposes an
    APM signal. Generic (documented limitation): specifics come from logs/events.
    """
    apm = telemetry.get("dynatrace") or telemetry.get("signalfx")
    if not apm:
        return None
    return {
        "signals": {
            "golden_signals": {
                "latency": {"p50": 400, "p95": 3000, "p99": 6000,
                            "baseline_p95": 200},
                "errors": {"rate": 0.08, "count": 120, "baseline_rate": 0.002},
                "saturation": {"cpu": 55, "memory": 60, "disk": 40},
                "traffic": {"rps": 480, "baseline_rps": 500},
            },
            "anomaly_detected": True,
            "anomaly_start": ANOMALY_TS,
            "anomaly_type": "degradation",
            "efic_context": _flatten(apm),
        }
    }


def _sysdig_metrics(telemetry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map sysdig/database numeric payloads into the doubly-nested metrics shape."""
    metrics: list[dict[str, Any]] = []
    pool_max = 0
    pattern = "spike"
    for src in ("sysdig", "database"):
        payload = telemetry.get(src)
        if not isinstance(payload, Mapping):
            continue
        for k in sorted(payload):
            v = payload[k]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                metrics.append({"name": f"{src}_{k}", "value": v,
                                "timestamp": BASE_TS})
                if k == "max":
                    pool_max = int(v)
            else:
                metrics.append({"name": f"{src}_{k}", "value": _flatten(v),
                                "timestamp": BASE_TS})
        if "rss" in payload or "trend" in payload:
            pattern = "gradual_increase"
        if "active" in payload and "max" in payload:
            pattern = "saturation"
    if not metrics:
        return None
    return {"metrics": {"metrics": metrics, "baseline": 0, "pattern": pattern,
                        "pool_max": pool_max}}


def _sysdig_events(telemetry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Render kubernetes/sysdig event payloads (OOMKilled, Evicted, ...)."""
    events: list[dict[str, Any]] = []
    for src in ("kubernetes", "sysdig"):
        payload = telemetry.get(src)
        if not isinstance(payload, Mapping):
            continue
        reason = payload.get("reason") or payload.get("event") or payload.get("msg")
        if reason:
            events.append({"message": f"{_flatten(reason)} :: {_flatten(payload)}",
                           "type": str(reason).lower(), "timestamp": BASE_TS})
    if not events:
        return None
    return {"events": events}


def _ci_details(incident: Mapping[str, Any],
                telemetry: Mapping[str, Any]) -> dict[str, Any]:
    service = str(incident.get("service", "unknown"))
    return {"ci": {"name": service, "sys_class_name": "cmdb_ci_service",
                   "environment": "production"}}


def render(task: Mapping[str, Any]) -> "RenderedScenario":
    """Render one EFIC task's PUBLIC telemetry into per-channel MCP responses.

    Uses only ``task.incident`` and ``task.telemetry``. Never reads
    ``ground_truth`` / ``traps`` / ``efic``.
    """
    incident = task.get("incident", {}) or {}
    telemetry = task.get("telemetry", {}) or {}

    logs, folded = _splunk_logs(incident, telemetry)
    channels: dict[str, dict[str, Any]] = {
        "moogsoft.get_incident_by_id": {"incident": {
            "incident_id": str(task.get("task_id", "")),
            "summary": str(incident.get("summary", "")),
            "affected_service": str(incident.get("service", "unknown")),
            "severity": incident.get("severity", 3),
            "status": "In Progress",
            "created_at": BASE_TS,
            "detected_at": BASE_TS,
        }},
        "splunk.search_logs": logs,
        "splunk.get_change_data": {"changes": _change_records(telemetry)},
        "servicenow.get_change_records": {"change_records": _change_records(telemetry)},
        "servicenow.get_ci_details": _ci_details(incident, telemetry),
    }
    gs = _golden_signals(telemetry)
    if gs is not None:
        channels["dynatrace.get_golden_signals"] = gs
    sm = _sysdig_metrics(telemetry)
    if sm is not None:
        channels["sysdig.query_metrics"] = sm
    se = _sysdig_events(telemetry)
    if se is not None:
        channels["sysdig.get_events"] = se

    # Provenance: which sources reached the engine, and how.
    provenance = {
        "native": sorted(s for s in telemetry if s in NATIVE_CHANNELS),
        "folded_to_logs": sorted(folded),
        "sources": sorted(telemetry),
    }
    return RenderedScenario(channels=channels, provenance=provenance)


class RenderedScenario:
    """The per-channel responses for one scenario + provenance metadata."""

    __slots__ = ("channels", "provenance")

    def __init__(self, *, channels: dict[str, dict[str, Any]],
                 provenance: dict[str, Any]) -> None:
        self.channels = channels
        self.provenance = provenance


__all__ = ["render", "RenderedScenario", "NATIVE_CHANNELS", "FOLDED_SOURCES",
           "BASE_TS"]
