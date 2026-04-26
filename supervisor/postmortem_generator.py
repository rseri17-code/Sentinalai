"""Blameless Postmortem Generator.

Auto-generates structured blameless postmortems from RCA investigation results.
Postmortems require human review/approval before publishing.

Lifecycle: draft → under_review → approved → published

Usage:
    from supervisor.postmortem_generator import generate_postmortem

    report = generate_postmortem(rca_result, resolved_at="2024-02-12T11:30:00Z")
    print(report.to_markdown())
    report.approve(reviewer="sre-lead")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.postmortem_generator")

# ---------------------------------------------------------------------------
# Contributing factors by incident type (systemic/process, never people)
# ---------------------------------------------------------------------------

_CONTRIBUTING_FACTORS: dict[str, list[str]] = {
    "timeout": [
        "Missing circuit breaker between the affected service and its dependency",
        "Alert threshold set too high — detection lag extended blast radius",
        "No per-call deadline budget enforced on outbound requests",
        "Retry logic without jitter amplified load on degraded downstream",
    ],
    "oomkill": [
        "Memory limits not aligned to observed peak usage patterns",
        "No memory-leak detection gate in CI/CD pipeline",
        "Container restart policy masked gradual leak — no heap dump captured",
        "VPA (Vertical Pod Autoscaler) not configured for this workload",
    ],
    "error_spike": [
        "Insufficient pre-deployment integration testing coverage",
        "No canary deployment gate with automated rollback",
        "Error budget not monitored continuously — delayed alert",
        "Feature flag rollout lacked incremental traffic analysis",
    ],
    "latency": [
        "No p95 latency SLO alert configured for the affected service",
        "Database query index degraded silently — no query-plan monitoring",
        "Connection pool sizing not reviewed after recent traffic growth",
        "Absence of read-replica lag monitoring",
    ],
    "saturation": [
        "No capacity headroom alert at 70% / 85% utilisation thresholds",
        "Autoscaling policy not tuned for the observed traffic pattern",
        "Resource quota set to match historical peak without growth buffer",
        "Shared infrastructure contention not tracked per-tenant",
    ],
    "network": [
        "Certificate lifecycle not automated — expiry unmonitored",
        "No external-dependency health check in readiness probe",
        "DNS resolution failure not distinguished from application errors in alerts",
        "mTLS rotation process lacks staged rollout verification",
    ],
    "cascading": [
        "Dependency graph not documented — blast radius unknown before incident",
        "No bulkhead isolation between service pools",
        "Shared cache eviction policy not tested under load",
        "Circuit breaker thresholds not calibrated to realistic failure rates",
    ],
    "missing_data": [
        "Data pipeline health not included in service SLO",
        "No stale-data alert on downstream consumers",
        "Observability gap — telemetry pipeline not monitored",
        "Partial failure handled silently without surfacing degraded state",
    ],
    "flapping": [
        "Liveness probe threshold too aggressive for observed startup variance",
        "No alert deduplication — notification fatigue masked root signal",
        "Intermittent failure not reproducible in staging — coverage gap",
        "Health check endpoint coupled to non-critical dependency",
    ],
    "silent_failure": [
        "Throughput monitoring absent — error rate alone insufficient",
        "Goroutine/thread leak not caught by existing memory alerts",
        "Queue depth not monitored — consumer stall invisible",
        "Black-box health check returned 200 while internal state was broken",
    ],
}

_WHAT_WENT_WELL_TEMPLATES = [
    "Automated alerting detected the incident without manual discovery",
    "On-call engineer responded within the SLA response window",
    "Runbook existed for this incident type and guided initial response",
    "Communication to stakeholders was timely and clear",
    "Rollback path was well understood and executed without hesitation",
]

_WHAT_TO_IMPROVE_BY_TYPE: dict[str, list[str]] = {
    "timeout": [
        "Manual identification of affected downstream services took too long — automate dependency map",
        "Timeout values were not consistently documented across service boundaries",
    ],
    "oomkill": [
        "Heap dump was not automatically captured at OOMKill — forensic data lost",
        "Memory limit increase required manual approval — add fast-path for P1 incidents",
    ],
    "error_spike": [
        "Rollback took longer than necessary — deploy pipeline not optimised for speed",
        "Post-deploy smoke tests did not cover the affected code path",
    ],
    "latency": [
        "Read-replica lag was not surfaced in the primary dashboard",
        "Query plan regression was only visible after manual investigation",
    ],
    "saturation": [
        "Scale-up operation required manual intervention — autoscaling lag extended incident",
        "Capacity forecast did not account for this traffic pattern",
    ],
    "network": [
        "Certificate expiry monitoring had a gap — inventory was stale",
        "Network-layer errors were not distinguished from application errors in initial triage",
    ],
    "cascading": [
        "Blast radius was larger than anticipated — CMDB dependency graph was outdated",
        "Circuit breakers were not enabled on all service-to-service calls",
    ],
    "default": [
        "Investigation relied on tribal knowledge — runbook needs updating",
        "Time to root cause exceeded target — additional tooling needed",
    ],
}

_ACTION_TEMPLATES: dict[str, list[dict]] = {
    "timeout": [
        {"title": "Add circuit breaker", "description": "Implement circuit breaker with 50% failure threshold on all outbound calls", "owner_team": "platform-team", "priority": "P1", "due_days": 7, "category": "prevention", "estimated_effort": "days"},
        {"title": "Instrument per-call deadlines", "description": "Enforce deadline budget propagation across all service boundaries via context", "owner_team": "sre-team", "priority": "P2", "due_days": 14, "category": "prevention", "estimated_effort": "days"},
        {"title": "Update timeout runbook", "description": "Document the causal chain and investigation steps in the runbook", "owner_team": "oncall-team", "priority": "P3", "due_days": 3, "category": "documentation", "estimated_effort": "hours"},
    ],
    "oomkill": [
        {"title": "Enable OOMKill heap dump capture", "description": "Configure automatic heap dump on OOMKill via JVM -XX:+HeapDumpOnOutOfMemoryError or Go pprof", "owner_team": "platform-team", "priority": "P1", "due_days": 7, "category": "detection", "estimated_effort": "days"},
        {"title": "Deploy VPA in recommendation mode", "description": "Enable Vertical Pod Autoscaler to right-size memory limits based on observed usage", "owner_team": "sre-team", "priority": "P2", "due_days": 14, "category": "prevention", "estimated_effort": "days"},
        {"title": "Add memory leak test to CI", "description": "Add integration test that runs service under load and validates heap growth stays bounded", "owner_team": "dev-team", "priority": "P2", "due_days": 21, "category": "prevention", "estimated_effort": "weeks"},
    ],
    "error_spike": [
        {"title": "Implement canary deployment gate", "description": "Add automated canary analysis (Argo Rollouts) with error-rate and latency gates before full rollout", "owner_team": "platform-team", "priority": "P1", "due_days": 14, "category": "prevention", "estimated_effort": "weeks"},
        {"title": "Add post-deploy smoke tests", "description": "Extend CI pipeline with integration smoke tests covering all critical user journeys", "owner_team": "dev-team", "priority": "P2", "due_days": 21, "category": "prevention", "estimated_effort": "weeks"},
        {"title": "Automate rollback trigger", "description": "Configure auto-rollback when error rate exceeds SLO threshold for > 5 minutes", "owner_team": "sre-team", "priority": "P1", "due_days": 7, "category": "response", "estimated_effort": "days"},
    ],
    "default": [
        {"title": "Update runbook", "description": "Document the investigation steps and resolution for this incident type", "owner_team": "oncall-team", "priority": "P2", "due_days": 5, "category": "documentation", "estimated_effort": "hours"},
        {"title": "Add detection alert", "description": "Create alert that would have detected this incident 15 minutes earlier", "owner_team": "sre-team", "priority": "P1", "due_days": 7, "category": "detection", "estimated_effort": "days"},
        {"title": "Conduct team review", "description": "Review incident timeline with the owning team to identify systemic improvements", "owner_team": "dev-team", "priority": "P3", "due_days": 14, "category": "prevention", "estimated_effort": "hours"},
    ],
}

_FIVE_WHYS_TEMPLATES: dict[str, list[str]] = {
    "timeout": [
        "Users experienced errors because requests to {service} timed out.",
        "Requests timed out because {service} was waiting on a slow downstream dependency.",
        "The downstream was slow because it was resource-constrained or overloaded.",
        "The overload was not detected early because no circuit breaker was in place.",
        "No circuit breaker existed because the dependency was considered low-risk and was not added to the resilience review checklist.",
    ],
    "oomkill": [
        "{service} was restarted by Kubernetes because it exceeded its memory limit.",
        "Memory exceeded the limit because of a gradual memory leak in the process.",
        "The leak was not detected before production because heap profiling is not part of CI.",
        "Heap profiling is not in CI because it was not prioritised in the engineering roadmap.",
        "It was not prioritised because there was no systematic review of OOMKill alert history to identify recurring patterns.",
    ],
    "error_spike": [
        "Users received errors because {service} was returning 5xx responses at high rate.",
        "{service} returned errors because a recent deployment introduced a bug on a code path not covered by tests.",
        "The bug reached production because post-deploy smoke tests did not exercise this path.",
        "Smoke tests did not cover this path because test coverage is not tied to deployment gates.",
        "Deployment gates do not enforce coverage requirements because the policy has not been enforced in the CI pipeline.",
    ],
    "default": [
        "The incident occurred because {service} failed to serve requests normally.",
        "{service} failed because an underlying resource or dependency became unavailable.",
        "The dependency became unavailable due to a change or degradation that was not anticipated.",
        "The degradation was not anticipated because monitoring coverage had a gap.",
        "The monitoring gap existed because the service risk model had not been reviewed recently.",
    ],
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ActionItem:
    title: str
    description: str
    owner_team: str
    priority: str
    due_days: int
    category: str
    estimated_effort: str


@dataclass
class PostmortemReport:
    incident_id: str
    generated_at: str
    incident_title: str
    severity: str
    duration_minutes: int
    executive_summary: str
    impact_statement: str
    timeline: list[dict]
    contributing_factors: list[str]
    what_went_well: list[str]
    what_needs_improvement: list[str]
    action_items: list[ActionItem]
    prevention_recommendations: list[str]
    similar_past_incidents: list[dict]
    five_whys: list[str]
    status: str = "draft"
    reviewed_by: str | None = None

    def approve(self, reviewer: str) -> None:
        self.status = "approved"
        self.reviewed_by = reviewer

    def to_markdown(self) -> str:
        lines: list[str] = [
            f"# Incident Postmortem: {self.incident_title}",
            "",
            f"**Incident ID:** {self.incident_id}  ",
            f"**Severity:** {self.severity}  ",
            f"**Duration:** {self.duration_minutes} minutes  ",
            f"**Status:** {self.status}  ",
            f"**Generated:** {self.generated_at}  ",
            "",
            "---",
            "",
            "## Executive Summary",
            "",
            self.executive_summary,
            "",
            "## Impact",
            "",
            self.impact_statement,
            "",
            "## Timeline",
            "",
        ]
        for event in self.timeline:
            t = event.get("time", "")
            desc = event.get("event", event.get("description", str(event)))
            src = event.get("source", "")
            lines.append(f"- **{t}** — {desc}" + (f" *(source: {src})*" if src else ""))
        lines += [
            "",
            "## Contributing Factors",
            "",
            *(f"- {f}" for f in self.contributing_factors),
            "",
            "## What Went Well",
            "",
            *(f"- {w}" for w in self.what_went_well),
            "",
            "## What Needs Improvement",
            "",
            *(f"- {w}" for w in self.what_needs_improvement),
            "",
            "## 5 Whys",
            "",
        ]
        for i, why in enumerate(self.five_whys, 1):
            lines.append(f"{i}. {why}")
        lines += [
            "",
            "## Action Items",
            "",
            "| Title | Owner | Priority | Due | Category | Effort |",
            "|---|---|---|---|---|---|",
        ]
        for ai in self.action_items:
            lines.append(f"| {ai.title} | {ai.owner_team} | {ai.priority} | +{ai.due_days}d | {ai.category} | {ai.estimated_effort} |")
        lines += [
            "",
            "## Prevention Recommendations",
            "",
            *(f"- {r}" for r in self.prevention_recommendations),
        ]
        if self.similar_past_incidents:
            lines += [
                "",
                "## Similar Past Incidents",
                "",
            ]
            for sim in self.similar_past_incidents:
                iid = sim.get("incident_id", "")
                rc = sim.get("root_cause", "")
                sim_score = sim.get("similarity", "")
                lines.append(f"- **{iid}**: {rc} (similarity: {sim_score})")
        if self.reviewed_by:
            lines += ["", f"*Approved by: {self.reviewed_by}*"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_postmortem(
    rca_result: dict[str, Any],
    resolved_at: str,
    team_notes: list[str] | None = None,
    similar_incidents: list[dict] | None = None,
) -> PostmortemReport:
    """Generate a blameless postmortem from an RCA investigation result.

    Args:
        rca_result:          The RCA output dict from SentinalAISupervisor.investigate().
        resolved_at:         ISO-8601 timestamp when the incident was resolved.
        team_notes:          Optional freeform notes from the responding team.
        similar_incidents:   Optional list of similar past incident dicts.

    Returns:
        PostmortemReport in 'draft' status — human review required before publishing.
    """
    now = datetime.now(timezone.utc).isoformat()

    incident_id   = rca_result.get("incident_id", "UNKNOWN")
    service       = rca_result.get("affected_service", rca_result.get("service", "unknown-service"))
    root_cause    = rca_result.get("root_cause", "undetermined")
    incident_type = rca_result.get("incident_type", "error_spike")
    severity      = rca_result.get("severity_label", rca_result.get("severity", "Unknown"))
    incident_summary = rca_result.get("incident_summary", rca_result.get("summary", f"{incident_type} on {service}"))
    confidence    = rca_result.get("confidence", 0)

    duration_minutes = _compute_duration(rca_result.get("start_time", ""), resolved_at)

    executive_summary = (
        f"{incident_type.replace('_', ' ').title()} incident affecting {service}. "
        f"Root cause: {root_cause}. "
        f"Duration: {duration_minutes} minutes. "
        f"Resolved at {resolved_at} with {confidence}% RCA confidence."
    )

    impact_statement = _build_impact_statement(severity, service, duration_minutes)

    timeline = list(rca_result.get("evidence_timeline", []))
    if team_notes:
        for note in team_notes:
            timeline.append({"time": now, "event": note, "source": "team-notes"})

    contributing_factors = list(_CONTRIBUTING_FACTORS.get(incident_type, _CONTRIBUTING_FACTORS.get("default", [])))
    if not contributing_factors:
        contributing_factors = _CONTRIBUTING_FACTORS.get("error_spike", [])

    what_went_well = list(_WHAT_WENT_WELL_TEMPLATES[:3])

    what_needs_improvement = list(
        _WHAT_TO_IMPROVE_BY_TYPE.get(incident_type, _WHAT_TO_IMPROVE_BY_TYPE["default"])
    )

    action_items = _build_action_items(incident_type, service)

    prevention_recommendations = _build_prevention_recommendations(
        incident_type, root_cause, service
    )

    five_whys = _build_five_whys(incident_type, service)

    return PostmortemReport(
        incident_id=incident_id,
        generated_at=now,
        incident_title=incident_summary,
        severity=severity,
        duration_minutes=duration_minutes,
        executive_summary=executive_summary,
        impact_statement=impact_statement,
        timeline=timeline,
        contributing_factors=contributing_factors,
        what_went_well=what_went_well,
        what_needs_improvement=what_needs_improvement,
        action_items=action_items,
        prevention_recommendations=prevention_recommendations,
        similar_past_incidents=list(similar_incidents) if similar_incidents else [],
        five_whys=five_whys,
        status="draft",
        reviewed_by=None,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_duration(start_time: str, resolved_at: str) -> int:
    """Return duration in minutes. Returns 0 if timestamps are unparseable."""
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end   = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
        delta = end - start
        return max(0, int(delta.total_seconds() / 60))
    except (ValueError, AttributeError, TypeError):
        return 0


def _build_impact_statement(severity: str, service: str, duration_minutes: int) -> str:
    sev_lower = severity.lower()
    if sev_lower in ("critical", "1"):
        scope = "all users of customer-facing features"
        level = "complete service unavailability"
    elif sev_lower in ("high", "2"):
        scope = "a significant subset of users"
        level = "severely degraded service"
    elif sev_lower in ("medium", "3"):
        scope = "users of specific features"
        level = "partial service degradation"
    else:
        scope = "a small number of users"
        level = "minor service degradation"
    return (
        f"{service} experienced {level} for {duration_minutes} minutes, "
        f"affecting {scope}. "
        f"Business impact includes potential SLA breach and customer trust risk."
    )


def _build_action_items(incident_type: str, service: str) -> list[ActionItem]:
    templates = _ACTION_TEMPLATES.get(incident_type, _ACTION_TEMPLATES["default"])
    items: list[ActionItem] = []
    for t in templates:
        items.append(ActionItem(
            title=t["title"],
            description=t["description"].replace("{service}", service),
            owner_team=t["owner_team"],
            priority=t["priority"],
            due_days=t["due_days"],
            category=t["category"],
            estimated_effort=t["estimated_effort"],
        ))
    return items


def _build_prevention_recommendations(incident_type: str, root_cause: str, service: str) -> list[str]:
    base = [
        f"Conduct a full review of {service} dependencies and add circuit breakers where missing.",
        "Ensure all alerts have a corresponding runbook entry with clear triage steps.",
        "Schedule a chaos engineering exercise targeting the failure mode exposed by this incident.",
    ]
    type_specific: dict[str, str] = {
        "timeout":     f"Implement bulkhead isolation for {service}'s downstream dependencies.",
        "oomkill":     f"Enable continuous heap profiling on {service} in production.",
        "error_spike": f"Add a feature-flag kill-switch to disable the affected code path instantly.",
        "latency":     f"Add a read-replica lag alert and automatic read/write splitting fallback.",
        "saturation":  f"Define and enforce autoscaling policies for {service} before next traffic spike.",
        "network":     "Automate certificate renewal and add 30-day expiry alerts.",
        "cascading":   "Document the full dependency graph in CMDB and keep it up to date.",
    }
    if incident_type in type_specific:
        base.insert(0, type_specific[incident_type])
    return base


def _build_five_whys(incident_type: str, service: str) -> list[str]:
    template = _FIVE_WHYS_TEMPLATES.get(incident_type, _FIVE_WHYS_TEMPLATES["default"])
    return [line.format(service=service) for line in template]
