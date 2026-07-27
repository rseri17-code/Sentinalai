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
    "cmdb": "servicenow.ci",               # EB-3: CMDB is served through ServiceNow CI
    "thousandeyes": "thousandeyes.network",  # EB-3: dedicated simulator (flag-gated)
}
# EFIC sources the UNMODIFIED engine has NO query path to (no worker/playbook/flag).
# EB-3: these are documented as engine-unreachable, NOT folded into another source
# (their observable symptom already reaches the engine via native EFIC-authored
# Splunk/ServiceNow telemetry). Simulating them would be unconsumed theater.
ENGINE_UNREACHABLE = (
    "certificates", "route53_dns", "identity", "aws_cloudwatch", "autosys",
    "network", "application",
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
                 telemetry: Mapping[str, Any]) -> dict[str, Any]:
    """Render Splunk search results from the scenario's NATIVE splunk telemetry.

    EB-3: no folding. Only EFIC's own splunk evidence (the observable log symptom
    the corpus authors) is rendered here — no other source is flattened into logs.
    """
    service = str(incident.get("service", "unknown"))
    results: list[dict[str, Any]] = []
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
    return {"logs": {"results": results, "count": len(results),
                     "first_occurrence": BASE_TS}}


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
    """ServiceNow CI details — the engine's production channel for CMDB data.

    EB-3: CMDB is not a distinct engine MCP; it is served through
    ``servicenow.get_ci_details``. Any EFIC ``cmdb`` evidence (config versions,
    registry reachability) is surfaced here — its production equivalent — instead
    of being folded into another source."""
    service = str(incident.get("service", "unknown"))
    ci: dict[str, Any] = {"name": service, "sys_class_name": "cmdb_ci_service",
                          "environment": "production"}
    cmdb = telemetry.get("cmdb")
    if isinstance(cmdb, Mapping):
        for k in sorted(cmdb):
            ci[k] = cmdb[k]
    return {"ci": ci}


def _route53_channels(telemetry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """IE-2: render Route53/DNS telemetry into the route53 MCP channels the DNS
    worker queries. Neutral facts only — the *verdict* (stale/outage) is left to
    the engine's `_analyze_dns` reasoning, never encoded here."""
    payload = telemetry.get("route53_dns")
    if not isinstance(payload, Mapping):
        return {}
    if "record" in payload or "points_to" in payload:
        record = {"name": payload.get("record", ""),
                  "points_to": payload.get("points_to", ""), "type": "CNAME"}
    else:
        record = None
    if "resolver" in payload or "query_timeouts" in payload:
        resolver = {"status": payload.get("resolver", "healthy"),
                    "query_timeouts": payload.get("query_timeouts", "low")}
    else:
        resolver = None
    return {"route53.get_record": {"record": record},
            "route53.check_resolver": {"resolver": resolver}}


def _dns_flag_on() -> bool:
    import os
    return os.environ.get("IE_DNS_ENABLED", "false").lower() in ("1", "true", "yes")


def _identity_flag_on() -> bool:
    import os
    return os.environ.get("IE_IDENTITY_ENABLED", "false").lower() in ("1", "true", "yes")


def _identity_channels(telemetry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """IE-3: render Identity/IAM telemetry into the identity MCP channels. Neutral
    facts only (IdP signing-key status, recent policy changes) — the authN-vs-authZ
    verdict is left to the engine's `_analyze_identity` reasoning."""
    payload = telemetry.get("identity")
    if not isinstance(payload, Mapping):
        return {}
    ch: dict[str, dict[str, Any]] = {}
    err = str(payload.get("error", "")).lower()
    if "kid" in payload or any(t in err for t in ("signing", "key", "token")):
        status = ("expired" if "expired" in err
                  else "invalid" if "invalid" in err else "healthy")
        ch["identity.check_token_signing"] = {
            "signing_key": {"kid": payload.get("kid", ""), "status": status}}
    else:
        ch["identity.check_token_signing"] = {"signing_key": None}
    if "policy_change" in payload:
        pc = payload["policy_change"]
        effect = ("deny" if any(t in str(pc).lower()
                                for t in ("remove", "revoke", "deny", "delete"))
                  else "allow")
        ch["identity.get_policy_changes"] = {
            "policy_changes": [{"change": pc, "effect": effect}]}
    else:
        ch["identity.get_policy_changes"] = {"policy_changes": []}
    return ch


def render(task: Mapping[str, Any]) -> "RenderedScenario":
    """Render one EFIC task's PUBLIC telemetry into per-channel MCP responses.

    Uses only ``task.incident`` and ``task.telemetry``. Never reads
    ``ground_truth`` / ``traps`` / ``efic``.
    """
    incident = task.get("incident", {}) or {}
    telemetry = task.get("telemetry", {}) or {}

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
        "splunk.search_logs": _splunk_logs(incident, telemetry),
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

    # EB-3: ThousandEyes — the one dedicated non-gateway simulator (flag-gated).
    from enterprisebench.pipeline.simulators import thousandeyes_responses
    te = thousandeyes_responses(telemetry.get("thousandeyes")) \
        if "thousandeyes" in telemetry else None

    # IE-2/IE-3: a source is engine-reachable only when its pilot flag is on. Flag
    # off ⇒ the source stays engine-unreachable and no channel is rendered, so the
    # flag-off report is byte-identical to the EB-3 baseline.
    now_reachable: set[str] = set()
    if _dns_flag_on():
        channels.update(_route53_channels(telemetry))
        now_reachable.add("route53_dns")
    if _identity_flag_on():
        channels.update(_identity_channels(telemetry))
        now_reachable.add("identity")

    unreachable = sorted(s for s in telemetry if s in ENGINE_UNREACHABLE
                         and s not in now_reachable)
    native = sorted([s for s in telemetry if s in NATIVE_CHANNELS]
                    + [s for s in now_reachable if s in telemetry])
    provenance = {
        "native_channel": native,
        "engine_unreachable": unreachable,
        "sources": sorted(telemetry),
        "folded_to_logs": [],   # EB-3: folding removed
    }
    return RenderedScenario(channels=channels, provenance=provenance, te=te)


class RenderedScenario:
    """The per-channel gateway responses for one scenario + provenance, plus the
    ThousandEyes responses (served outside the gateway, via the TE adapter)."""

    __slots__ = ("channels", "provenance", "te")

    def __init__(self, *, channels: dict[str, dict[str, Any]],
                 provenance: dict[str, Any],
                 te: dict[str, Any] | None = None) -> None:
        self.channels = channels
        self.provenance = provenance
        self.te = te


__all__ = ["render", "RenderedScenario", "NATIVE_CHANNELS", "ENGINE_UNREACHABLE",
           "BASE_TS"]
