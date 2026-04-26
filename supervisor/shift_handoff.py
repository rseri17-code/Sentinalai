"""Shift Handoff Intelligence Brief.

Generates a structured handoff brief for incoming SRE shifts.
Analyzes the last N days of incidents and upcoming changes to produce
a concise intelligence package for the incoming engineer.

Usage:
    from supervisor.shift_handoff import generate_handoff_brief

    brief = generate_handoff_brief(
        experiences=experience_store.get_all(),
        active_incidents=[...],
        upcoming_changes=[...],
        outgoing_engineer="alice",
        incoming_engineer="bob",
    )
    print(brief.to_slack_message())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger("sentinalai.shift_handoff")

# ---------------------------------------------------------------------------
# Watch signals per incident type
# ---------------------------------------------------------------------------

_WATCH_SIGNALS: dict[str, list[str]] = {
    "timeout":       ["DB query latency p95", "connection pool utilisation", "upstream error rate"],
    "oomkill":       ["heap usage %", "GC pause time ms", "container restart count"],
    "error_spike":   ["error rate %", "deployment age (hours since last deploy)", "p99 latency"],
    "latency":       ["response time p95/p99", "DB replication lag seconds", "cache hit rate"],
    "saturation":    ["CPU utilisation %", "thread pool queue depth", "disk IOPS utilisation"],
    "network":       ["DNS resolution error rate", "TLS handshake failures", "packet loss %"],
    "cascading":     ["circuit breaker state", "downstream error rates", "connection pool across services"],
    "missing_data":  ["data pipeline lag seconds", "consumer group offset lag", "telemetry gap alerts"],
    "flapping":      ["restart count (last 1h)", "readiness probe failure rate", "health check p99"],
    "silent_failure": ["request throughput (rps)", "queue depth", "goroutine/thread count"],
}

# Conditional guidance templates per incident type
_CONDITIONAL_GUIDANCE: dict[str, tuple[str, str, str]] = {
    "timeout":       ("If {service} error rate > 5% and no recent deploy",
                      "Check DB connection pool utilisation first, then downstream service latency",
                      "See runbook/timeout-cascade"),
    "oomkill":       ("If {service} memory > 85% and trending up",
                      "Capture heap dump immediately (kubectl exec), then scale up replicas",
                      "See runbook/oomkill-response"),
    "error_spike":   ("If {service} error rate spikes after deploy",
                      "Rollback deployment immediately via ArgoCD, then investigate logs",
                      "See runbook/deployment-rollback"),
    "latency":       ("If {service} p95 latency > 2× baseline",
                      "Check read-replica lag first, then query execution plans",
                      "See runbook/latency-investigation"),
    "saturation":    ("If {service} CPU > 80% for > 5 minutes",
                      "Scale out horizontally first, then identify the hot thread via profiler",
                      "See runbook/cpu-saturation"),
    "network":       ("If {service} connection refused errors spike",
                      "Verify DNS resolution, then check TLS certificate expiry dates",
                      "See runbook/network-triage"),
    "cascading":     ("If multiple services degrade simultaneously",
                      "Check shared resources first (DB, cache, DNS) — isolate the origin service",
                      "See runbook/cascading-failure"),
    "silent_failure": ("If {service} throughput drops with no error increase",
                       "Check consumer group lag and goroutine count — likely upstream stall",
                       "See runbook/silent-failure"),
}

# Risk level by change type
_CHANGE_RISK: dict[str, str] = {
    "database_migration": "high",
    "db_migration":       "high",
    "schema_migration":   "high",
    "deployment":         "medium",
    "release":            "medium",
    "config_change":      "medium",
    "infrastructure":     "medium",
    "maintenance":        "low",
    "certificate_renewal": "low",
    "scaling":            "low",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FragileService:
    service: str
    reason: str
    incident_count_7d: int
    last_incident_type: str
    risk_level: str
    watch_signals: list[str]


@dataclass
class ConditionalGuidance:
    trigger: str
    action: str
    escalate_to: str
    runbook_hint: str


@dataclass
class HandoffBrief:
    generated_at: str
    shift_start: str
    outgoing_engineer: str
    incoming_engineer: str
    fragile_services: list[FragileService]
    active_investigations: list[dict]
    watch_items: list[str]
    upcoming_risk: list[dict]
    conditional_guidance: list[ConditionalGuidance]
    open_action_items: list[dict]
    summary: str

    def to_slack_message(self) -> str:
        lines: list[str] = [
            f"SHIFT HANDOFF — {self.shift_start}",
            f"Outgoing: {self.outgoing_engineer}  |  Incoming: {self.incoming_engineer}",
            "---",
        ]

        if self.summary:
            lines += ["SUMMARY", self.summary, "---"]

        if self.fragile_services:
            lines.append("FRAGILE SERVICES (elevated attention required)")
            for svc in self.fragile_services:
                lines.append(
                    f"  [{svc.risk_level.upper()}] {svc.service} — "
                    f"{svc.incident_count_7d} incidents in 7d — {svc.reason}"
                )
            lines.append("---")

        if self.active_investigations:
            lines.append("ACTIVE INVESTIGATIONS")
            for inv in self.active_investigations:
                iid = inv.get("incident_id", inv.get("id", "?"))
                status = inv.get("status", "open")
                lines.append(f"  {iid}: {status}")
            lines.append("---")

        if self.watch_items:
            lines.append("WATCH LIST")
            for item in self.watch_items:
                lines.append(f"  - {item}")
            lines.append("---")

        if self.upcoming_risk:
            lines.append("UPCOMING CHANGES (next 8h)")
            for change in self.upcoming_risk:
                svc = change.get("service", "?")
                ct = change.get("change_type", "?")
                at = change.get("scheduled_at", "?")
                risk = change.get("risk_level", "?")
                lines.append(f"  [{risk.upper()}] {svc}: {ct} at {at}")
            lines.append("---")

        if self.conditional_guidance:
            lines.append("IF/THEN GUIDANCE")
            for g in self.conditional_guidance:
                lines.append(f"  IF: {g.trigger}")
                lines.append(f"  DO: {g.action}")
                lines.append(f"  REF: {g.runbook_hint}")
                lines.append("")

        lines.append(f"Generated by SentinalAI at {self.generated_at}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_handoff_brief(
    experiences: list[dict],
    active_incidents: list[dict],
    upcoming_changes: list[dict],
    outgoing_engineer: str = "outgoing-sre",
    incoming_engineer: str = "incoming-sre",
    lookback_days: int = 7,
) -> HandoffBrief:
    """Generate a shift handoff intelligence brief.

    Args:
        experiences:        Recent experience dicts from the experience store.
        active_incidents:   Currently open or in-progress incidents.
        upcoming_changes:   Scheduled ITSM change records in the next shift window.
        outgoing_engineer:  Name of the engineer going off-call.
        incoming_engineer:  Name of the incoming engineer.
        lookback_days:      How many days back to scan for fragile services.

    Returns:
        HandoffBrief ready to post to Slack or render as a document.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    now_iso = now.isoformat()

    # ------------------------------------------------------------------
    # 1. Find fragile services from experience history
    # ------------------------------------------------------------------
    service_incidents: dict[str, list[dict]] = {}
    for exp in experiences:
        ts_str = exp.get("timestamp", "")
        ts = _parse_ts(ts_str)
        if ts is None or ts < cutoff:
            continue
        svc = exp.get("service", "unknown")
        service_incidents.setdefault(svc, []).append(exp)

    fragile_services: list[FragileService] = []
    for svc, incs in service_incidents.items():
        count = len(incs)
        if count < 2:
            continue
        last_type = incs[-1].get("incident_type", "unknown")
        if count >= 4:
            risk_level = "critical"
        elif count >= 3:
            risk_level = "high"
        else:
            risk_level = "elevated"

        reason = (
            f"{count} incidents in the last {lookback_days} days "
            f"(latest: {last_type.replace('_', ' ')})"
        )
        watch_signals = _WATCH_SIGNALS.get(last_type, ["error rate", "latency p95", "CPU utilisation"])
        fragile_services.append(FragileService(
            service=svc,
            reason=reason,
            incident_count_7d=count,
            last_incident_type=last_type,
            risk_level=risk_level,
            watch_signals=watch_signals,
        ))

    fragile_services.sort(key=lambda s: s.incident_count_7d, reverse=True)

    # ------------------------------------------------------------------
    # 2. Watch items — pull from top fragile services
    # ------------------------------------------------------------------
    watch_items: list[str] = []
    for fs in fragile_services[:3]:
        for signal in fs.watch_signals[:2]:
            item = f"{fs.service}: {signal}"
            if item not in watch_items:
                watch_items.append(item)

    # ------------------------------------------------------------------
    # 3. Conditional guidance for each fragile service
    # ------------------------------------------------------------------
    conditional_guidance: list[ConditionalGuidance] = []
    seen_types: set[str] = set()
    for fs in fragile_services:
        itype = fs.last_incident_type
        if itype in seen_types or itype not in _CONDITIONAL_GUIDANCE:
            continue
        seen_types.add(itype)
        trigger_tmpl, action, runbook = _CONDITIONAL_GUIDANCE[itype]
        conditional_guidance.append(ConditionalGuidance(
            trigger=trigger_tmpl.format(service=fs.service),
            action=action,
            escalate_to="sre-oncall",
            runbook_hint=runbook,
        ))

    # ------------------------------------------------------------------
    # 4. Upcoming risk from ITSM changes
    # ------------------------------------------------------------------
    upcoming_risk: list[dict] = []
    for change in upcoming_changes:
        ct = change.get("change_type", change.get("type", "deployment")).lower()
        risk = _CHANGE_RISK.get(ct, "medium")
        upcoming_risk.append({
            "service":       change.get("service", change.get("affected_service", "unknown")),
            "change_type":   ct,
            "scheduled_at":  change.get("scheduled_at", change.get("start_date", "TBD")),
            "risk_level":    risk,
            "description":   change.get("description", change.get("short_description", "")),
        })
    upcoming_risk.sort(key=lambda c: {"high": 0, "medium": 1, "low": 2}.get(c["risk_level"], 1))

    # ------------------------------------------------------------------
    # 5. Summary narrative
    # ------------------------------------------------------------------
    summary = _build_summary(
        fragile_services, active_incidents, upcoming_risk, lookback_days
    )

    return HandoffBrief(
        generated_at=now_iso,
        shift_start=now_iso,
        outgoing_engineer=outgoing_engineer,
        incoming_engineer=incoming_engineer,
        fragile_services=fragile_services,
        active_investigations=list(active_incidents),
        watch_items=watch_items,
        upcoming_risk=upcoming_risk,
        conditional_guidance=conditional_guidance,
        open_action_items=[],
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_summary(
    fragile: list[FragileService],
    active: list[dict],
    upcoming: list[dict],
    lookback_days: int,
) -> str:
    parts: list[str] = []

    if fragile:
        names = ", ".join(f.service for f in fragile[:3])
        parts.append(
            f"{len(fragile)} service(s) are elevated after the last {lookback_days} days: {names}."
        )
    else:
        parts.append(f"No recurring incident patterns detected in the last {lookback_days} days.")

    if active:
        parts.append(f"{len(active)} investigation(s) are currently open — review before diving into new work.")
    else:
        parts.append("No active investigations at shift start.")

    high_risk = [c for c in upcoming if c.get("risk_level") == "high"]
    if high_risk:
        svc_list = ", ".join(c["service"] for c in high_risk[:3])
        parts.append(
            f"High-risk changes are scheduled this shift for: {svc_list} — ensure rollback plans are ready."
        )
    elif upcoming:
        parts.append(f"{len(upcoming)} change(s) scheduled this shift — monitor closely after each window.")
    else:
        parts.append("No changes scheduled for this shift.")

    return " ".join(parts)


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None
