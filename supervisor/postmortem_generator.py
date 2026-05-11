"""Postmortem auto-draft generator for SentinalAI.

Generates blameless postmortem documents from RCA investigation results.
The report is structured to cover SRE best practices:
  - Executive summary (impact + duration)
  - Chronological timeline from evidence_timeline
  - 5 Whys causal chain
  - Contributing factors
  - What went well / what needs improvement
  - Action items (prioritised, categorised, with due dates)
  - Prevention recommendations

The generator works offline — it does NOT require an LLM call.
If LLM is available and POSTMORTEM_LLM_ENRICH=true, it enriches the
executive summary and 5 Whys using the configured LLM.

Usage:
    from supervisor.postmortem_generator import generate_postmortem

    report = generate_postmortem(
        rca_result=result_dict,
        resolved_at="2026-01-15T10:45:00Z",
        team_notes=["We noticed the issue 2 minutes after deploy"],
        similar_incidents=["INC-4201", "INC-3978"],
    )
    print(report.to_markdown())
    report.approve(reviewer="alice")
"""
from __future__ import annotations

import logging
import os
import re
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("sentinalai.postmortem_generator")

POSTMORTEM_LLM_ENRICH = os.environ.get("POSTMORTEM_LLM_ENRICH", "false").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ActionItem:
    title: str
    description: str = ""
    priority: str = "P3"       # P1 / P2 / P3
    category: str = "prevention"  # prevention / detection / response / process
    owner: str = "sre-team"
    due_days: int = 30
    estimated_effort: str = "days"  # hours / days / weeks


@dataclass
class PostmortemReport:
    report_id: str
    incident_id: str
    affected_service: str
    severity: str
    status: str = "draft"  # draft / approved / published
    reviewed_by: Optional[str] = None
    generated_at: str = ""

    # Impact
    duration_minutes: float = 0.0
    start_time: str = ""
    resolved_at: str = ""

    # Content sections
    executive_summary: str = ""
    impact_statement: str = ""
    timeline: list[dict] = field(default_factory=list)
    contributing_factors: list[str] = field(default_factory=list)
    what_went_well: list[str] = field(default_factory=list)
    what_needs_improvement: list[str] = field(default_factory=list)
    five_whys: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    prevention_recommendations: list[str] = field(default_factory=list)
    similar_past_incidents: list[str] = field(default_factory=list)

    def approve(self, reviewer: str) -> None:
        self.status = "approved"
        self.reviewed_by = reviewer

    def to_markdown(self) -> str:
        """Render the postmortem as a Markdown document."""
        lines: list[str] = []
        _h1 = lambda t: lines.append(f"# {t}\n")
        _h2 = lambda t: lines.append(f"\n## {t}\n")
        _li = lambda t: lines.append(f"- {t}")
        _nl = lambda: lines.append("")

        _h1(f"Postmortem — {self.incident_id} — {self.affected_service}")
        lines.append(f"**Generated:** {self.generated_at}")
        lines.append(f"**Status:** {self.status}")
        if self.reviewed_by:
            lines.append(f"**Reviewed by:** {self.reviewed_by}")
        lines.append(f"**Severity:** {self.severity}")
        lines.append(f"**Duration:** {self.duration_minutes:.0f} minutes")
        _nl()

        _h2("Executive Summary")
        lines.append(self.executive_summary or "_No summary available._")

        _h2("Impact")
        lines.append(self.impact_statement or "_Impact not specified._")

        _h2("Timeline")
        if self.timeline:
            for entry in self.timeline:
                ts = entry.get("ts", entry.get("timestamp", "?"))
                desc = entry.get("description", entry.get("event", ""))
                lines.append(f"- **{ts}** — {desc}")
        else:
            lines.append("_No timeline entries available._")

        _h2("Five Whys")
        for i, why in enumerate(self.five_whys, 1):
            lines.append(f"{i}. {why}")
        if not self.five_whys:
            lines.append("_Five Whys analysis not available._")

        _h2("Contributing Factors")
        for f in self.contributing_factors:
            _li(f)
        if not self.contributing_factors:
            _li("_Not yet identified_")

        _h2("What Went Well")
        for item in self.what_went_well:
            _li(item)

        _h2("What Needs Improvement")
        for item in self.what_needs_improvement:
            _li(item)

        _h2("Action Items")
        if self.action_items:
            lines.append("| Priority | Title | Owner | Due | Effort | Category |")
            lines.append("|---|---|---|---|---|---|")
            for ai in self.action_items:
                due = f"{ai.due_days}d"
                lines.append(f"| {ai.priority} | {ai.title} | {ai.owner} | {due} | {ai.estimated_effort} | {ai.category} |")
        else:
            lines.append("_No action items defined._")

        _h2("Prevention Recommendations")
        for rec in self.prevention_recommendations:
            _li(rec)

        if self.similar_past_incidents:
            _h2("Similar Past Incidents")
            for inc in self.similar_past_incidents:
                _li(inc)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_postmortem(
    rca_result: dict[str, Any],
    resolved_at: str = "",
    team_notes: list[str] | None = None,
    similar_incidents: list[str] | None = None,
) -> PostmortemReport:
    """Generate a PostmortemReport from an RCA result dict.

    Args:
        rca_result:        Dict from supervisor.agent.investigate() or experience store.
        resolved_at:       ISO-8601 timestamp when the incident was resolved.
        team_notes:        Human-authored notes to fold into the report.
        similar_incidents: IDs of related past incidents (from experience store).

    Returns:
        PostmortemReport dataclass (status="draft", not yet approved).
    """
    team_notes = team_notes or []
    similar_incidents = similar_incidents or []

    incident_id = str(rca_result.get("incident_id", "UNKNOWN"))
    service = str(rca_result.get("affected_service", rca_result.get("service", "unknown")))
    severity = str(rca_result.get("severity_label", rca_result.get("severity", "unknown")))
    root_cause = str(rca_result.get("root_cause", ""))
    fix_applied = str(rca_result.get("fix_applied", rca_result.get("fix", "")))
    confidence = float(rca_result.get("confidence", rca_result.get("confidence_calibrated", 0)))
    start_time = str(rca_result.get("start_time", rca_result.get("created_at", "")))
    evidence_timeline: list[dict] = rca_result.get("evidence_timeline", [])

    resolved_at = resolved_at or datetime.now(timezone.utc).isoformat()
    duration_minutes = _compute_duration(start_time, resolved_at)

    report_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()

    # ── Executive summary ──────────────────────────────────────────────────
    executive_summary = _build_executive_summary(
        incident_id, service, severity, root_cause, duration_minutes, confidence, fix_applied
    )

    # ── Impact statement ───────────────────────────────────────────────────
    impact_statement = _build_impact_statement(rca_result, duration_minutes)

    # ── Timeline ───────────────────────────────────────────────────────────
    timeline = _build_timeline(evidence_timeline, start_time, resolved_at, team_notes)

    # ── Five Whys ──────────────────────────────────────────────────────────
    five_whys = _build_five_whys(root_cause, rca_result)

    # ── Contributing factors ───────────────────────────────────────────────
    contributing_factors = _build_contributing_factors(rca_result)

    # ── What went well / needs improvement ────────────────────────────────
    what_went_well = _build_went_well(rca_result, team_notes)
    what_needs_improvement = _build_needs_improvement(rca_result, duration_minutes)

    # ── Action items ───────────────────────────────────────────────────────
    action_items = _build_action_items(root_cause, fix_applied, rca_result, duration_minutes)

    # ── Prevention recommendations ─────────────────────────────────────────
    prevention = _build_prevention(root_cause, rca_result)

    report = PostmortemReport(
        report_id=report_id,
        incident_id=incident_id,
        affected_service=service,
        severity=severity,
        generated_at=generated_at,
        duration_minutes=round(duration_minutes, 1),
        start_time=start_time,
        resolved_at=resolved_at,
        executive_summary=executive_summary,
        impact_statement=impact_statement,
        timeline=timeline,
        contributing_factors=contributing_factors,
        what_went_well=what_went_well,
        what_needs_improvement=what_needs_improvement,
        five_whys=five_whys,
        action_items=action_items,
        prevention_recommendations=prevention,
        similar_past_incidents=similar_incidents,
    )

    if POSTMORTEM_LLM_ENRICH:
        _enrich_with_llm(report, rca_result)

    logger.info(
        "Postmortem generated: report=%s incident=%s service=%s duration=%.0fmin confidence=%.0f",
        report_id, incident_id, service, duration_minutes, confidence,
    )
    return report


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_executive_summary(
    incident_id: str, service: str, severity: str, root_cause: str,
    duration_minutes: float, confidence: float, fix_applied: str,
) -> str:
    dur = _format_duration(duration_minutes)
    conf_note = f" (confidence: {confidence:.0f}%)" if confidence > 0 else ""
    summary_parts = [
        f"On {service}, incident {incident_id} caused a {severity}-severity outage lasting {dur}.",
    ]
    if root_cause:
        truncated = root_cause[:300].rstrip(".")
        summary_parts.append(f"Root cause{conf_note}: {truncated}.")
    if fix_applied:
        summary_parts.append(f"Resolution: {fix_applied[:200]}.")
    return " ".join(summary_parts)


def _build_impact_statement(rca: dict, duration_minutes: float) -> str:
    service = rca.get("affected_service", "the affected service")
    severity = rca.get("severity_label", rca.get("severity", "unknown"))
    parts = [f"{service} experienced a {severity}-severity incident."]
    if duration_minutes > 0:
        parts.append(f"Total duration: {_format_duration(duration_minutes)}.")
    error_rate = rca.get("error_rate_pct") or rca.get("error_rate")
    if error_rate:
        parts.append(f"Peak error rate: {error_rate}%.")
    affected_users = rca.get("affected_users") or rca.get("users_impacted")
    if affected_users:
        parts.append(f"Estimated users impacted: {affected_users}.")
    return " ".join(parts)


def _build_timeline(
    evidence: list[dict], start_time: str, resolved_at: str, team_notes: list[str]
) -> list[dict]:
    entries: list[dict] = []

    if start_time:
        entries.append({"ts": _fmt_ts(start_time), "description": "Incident detected / alerts fired"})

    for ev in evidence:
        ts = ev.get("timestamp", ev.get("ts", ev.get("time", "")))
        desc = ev.get("description", ev.get("event", ev.get("summary", "")))
        source = ev.get("source", ev.get("tool", ""))
        if desc:
            entries.append({
                "ts": _fmt_ts(ts) if ts else "—",
                "description": f"[{source}] {desc}" if source else desc,
            })

    for note in team_notes:
        entries.append({"ts": "operator note", "description": note})

    if resolved_at:
        entries.append({"ts": _fmt_ts(resolved_at), "description": "Incident resolved / service restored"})

    return entries


def _build_five_whys(root_cause: str, rca: dict) -> list[str]:
    """Construct a 5 Whys chain from the root cause and evidence."""
    service = rca.get("affected_service", "the service")
    incident_type = rca.get("incident_type", "")

    if not root_cause:
        return [
            f"Why did {service} fail? — Investigation did not produce a root cause.",
            "Why is root cause unknown? — Evidence was insufficient.",
            "Why was evidence insufficient? — Observability gaps may exist.",
            "Why do observability gaps exist? — Monitoring coverage needs review.",
            "Why hasn't this been fixed? — Add this to action items.",
        ]

    sentences = [s.strip() for s in re.split(r"[.!?]+", root_cause) if s.strip()]
    first = sentences[0] if sentences else root_cause[:150]

    whys = [
        f"Why did the incident occur? — {first}.",
    ]

    # Detect common patterns and chain accordingly
    if "timeout" in root_cause.lower() or "latency" in root_cause.lower():
        whys += [
            f"Why did timeouts occur? — Request processing exceeded configured limits under load.",
            f"Why was the system under excessive load? — Insufficient capacity or upstream dependency failure.",
            f"Why wasn't capacity adequate? — Autoscaling policy did not react fast enough.",
            f"Why didn't autoscaling react fast enough? — Metrics lag or scale-up threshold too conservative.",
        ]
    elif "memory" in root_cause.lower() or "oom" in root_cause.lower():
        whys += [
            f"Why did memory grow unbounded? — A memory leak prevented garbage collection.",
            f"Why was the leak not caught earlier? — No memory growth alert was configured.",
            f"Why was there no alert? — Memory alerting was not part of the onboarding checklist.",
            f"Why not? — The runbook for new services lacked memory observability requirements.",
        ]
    elif "deploy" in root_cause.lower() or "regression" in root_cause.lower():
        whys += [
            f"Why did the deployment cause regression? — The change was not caught by pre-production testing.",
            f"Why did testing miss it? — Load or integration tests did not cover the affected code path.",
            f"Why was coverage missing? — Test suite was not updated when the feature was added.",
            f"Why wasn't test coverage enforced? — No coverage gate exists in the deployment pipeline.",
        ]
    else:
        whys += [
            f"Why did this condition exist? — The underlying system state allowed failure to propagate.",
            f"Why did failure propagate? — No circuit breaker or fallback was in place.",
            f"Why was there no circuit breaker? — The dependency was not classified as a failure domain.",
            f"Why was it not classified? — Dependency mapping in CMDB was incomplete.",
        ]

    return whys[:5]


def _build_contributing_factors(rca: dict) -> list[str]:
    factors = []
    root_cause = (rca.get("root_cause") or "").lower()
    hypothesis = (rca.get("hypothesis") or "").lower()
    combined = root_cause + " " + hypothesis

    if any(w in combined for w in ["alert", "monitor", "observ"]):
        factors.append("Delayed alerting — alert thresholds not tuned to detect the failure mode early")
    if any(w in combined for w in ["deploy", "release", "rollout"]):
        factors.append("Insufficient pre-production validation — regression not caught in staging")
    if any(w in combined for w in ["timeout", "circuit", "retry"]):
        factors.append("Missing resilience pattern — no circuit breaker or fallback for the dependency")
    if any(w in combined for w in ["capacity", "scale", "load"]):
        factors.append("Capacity planning gap — growth not anticipated in resource provisioning")
    if any(w in combined for w in ["config", "setting", "env"]):
        factors.append("Configuration management — environment-specific config not validated before deploy")

    if not factors:
        factors = [
            "Contributing factors could not be automatically determined — review root cause analysis",
            "Manual review of the evidence timeline is recommended",
        ]
    return factors


def _build_went_well(rca: dict, team_notes: list[str]) -> list[str]:
    items = []
    confidence = float(rca.get("confidence", rca.get("confidence_calibrated", 0)))
    elapsed_ms = float(rca.get("elapsed_ms", 0))

    if confidence >= 70:
        items.append(f"RCA produced high-confidence root cause ({confidence:.0f}%)")
    if elapsed_ms > 0 and elapsed_ms < 60000:
        items.append(f"Automated investigation completed in {elapsed_ms/1000:.0f}s")
    if rca.get("experience_stored"):
        items.append("Investigation outcome was stored for future reference")
    if rca.get("experience_matches", 0) > 0:
        items.append(f"Historical experience matched ({rca['experience_matches']} similar incidents found)")
    for note in team_notes:
        if any(w in note.lower() for w in ["good", "well", "fast", "quick", "correct"]):
            items.append(note)

    if not items:
        items = [
            "Incident was detected and investigated promptly",
            "SentinalAI autonomous RCA completed without manual escalation",
        ]
    return items


def _build_needs_improvement(rca: dict, duration_minutes: float) -> list[str]:
    items = []
    confidence = float(rca.get("confidence", rca.get("confidence_calibrated", 0)))
    rounds = int(rca.get("rounds_run", 1))
    stuck = bool(rca.get("stuck", False))

    if confidence < 60:
        items.append(f"RCA confidence was low ({confidence:.0f}%) — improve observability coverage")
    if stuck:
        items.append("Investigation got stuck — evidence gaps prevented confident root cause identification")
    if rounds >= 4:
        items.append(f"Investigation required {rounds} refinement rounds — initial hypothesis quality needs improvement")
    if duration_minutes > 60:
        items.append(f"Incident duration was {duration_minutes:.0f} minutes — detection or response time needs improvement")

    if not items:
        items = ["Review post-incident runbook compliance", "Validate alert routing was correct"]
    return items


def _build_action_items(
    root_cause: str, fix_applied: str, rca: dict, duration_minutes: float
) -> list[ActionItem]:
    items: list[ActionItem] = []
    combined = (root_cause + " " + fix_applied).lower()

    # Immediate fix action
    if fix_applied:
        items.append(ActionItem(
            title="Implement permanent fix",
            description=f"Apply and validate: {fix_applied[:200]}",
            priority="P1",
            category="prevention",
            due_days=7,
            estimated_effort="days",
        ))

    # Alerting
    if "alert" not in combined or "monitor" not in combined:
        items.append(ActionItem(
            title="Add/tune alert for this failure mode",
            description="Ensure an alert fires within 2 minutes of the symptoms that triggered this incident",
            priority="P2",
            category="detection",
            due_days=14,
            estimated_effort="hours",
        ))

    # Resilience
    if any(w in combined for w in ["timeout", "dependency", "upstream", "cascade"]):
        items.append(ActionItem(
            title="Add circuit breaker / fallback",
            description="Implement resilience pattern to prevent cascading failure on upstream degradation",
            priority="P2",
            category="prevention",
            due_days=21,
            estimated_effort="days",
        ))

    # Runbook
    items.append(ActionItem(
        title="Update runbook with this incident pattern",
        description=f"Document detection signals, root cause pattern, and resolution steps for {rca.get('incident_type', 'this failure mode')}",
        priority="P3",
        category="process",
        owner="sre-team",
        due_days=30,
        estimated_effort="hours",
    ))

    # MTTR improvement if long
    if duration_minutes > 45:
        items.append(ActionItem(
            title="Reduce MTTR — automate recovery for this pattern",
            description="Script the resolution steps so on-call can execute in <5 minutes",
            priority="P2",
            category="response",
            due_days=30,
            estimated_effort="days",
        ))

    # Post-incident review scheduling
    items.append(ActionItem(
        title="Schedule blameless post-incident review",
        description="30-minute team sync to review this postmortem, validate action items, and capture lessons",
        priority="P3",
        category="process",
        due_days=7,
        estimated_effort="hours",
    ))

    return items


def _build_prevention(root_cause: str, rca: dict) -> list[str]:
    combined = (root_cause + " " + rca.get("incident_type", "")).lower()
    recs = []

    if any(w in combined for w in ["memory", "heap", "oom", "leak"]):
        recs.append("Implement memory growth alerts (alert at 80% heap, page at 95%)")
        recs.append("Add load testing with memory profiling to pre-production pipeline")
    if any(w in combined for w in ["deploy", "release", "regression"]):
        recs.append("Add canary deployment gate — require 5 minutes clean at 1% traffic before full rollout")
        recs.append("Implement automated rollback trigger when error rate exceeds 1% post-deploy")
    if any(w in combined for w in ["database", "db", "connection", "query"]):
        recs.append("Enable slow query logging with alert on P95 > 500ms")
        recs.append("Implement read replica routing for analytics workloads")
    if any(w in combined for w in ["cert", "tls", "ssl", "expir"]):
        recs.append("Implement cert-manager with automated renewal 30 days before expiry")
        recs.append("Add certificate expiry monitoring with P1 alert at <7 days remaining")
    if any(w in combined for w in ["dns", "resolution", "network"]):
        recs.append("Add DNS resolution health checks to synthetic monitoring")
        recs.append("Implement DNS TTL monitoring to detect stale record issues")

    if not recs:
        recs = [
            "Add observability coverage for the failure mode identified in this incident",
            "Include this failure scenario in the next chaos engineering exercise",
            "Review dependency blast radius and implement appropriate circuit breakers",
        ]
    return recs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_duration(start: str, end: str) -> float:
    """Return duration in minutes between two ISO-8601 timestamps."""
    try:
        t_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0.0, (t_end - t_start).total_seconds() / 60.0)
    except Exception:
        return 0.0


def _format_duration(minutes: float) -> str:
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes:.0f} minutes"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def _fmt_ts(ts: str) -> str:
    """Reformat ISO timestamp to a human-readable form."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ts[:19] if len(ts) >= 19 else ts


def _enrich_with_llm(report: PostmortemReport, rca: dict) -> None:
    """Optionally enrich executive summary and 5 Whys with LLM."""
    try:
        from supervisor.llm import call_llm
        prompt = (
            f"You are writing a blameless postmortem for incident {report.incident_id}.\n"
            f"Root cause: {rca.get('root_cause', 'unknown')}\n"
            f"Service: {report.affected_service}, Severity: {report.severity}\n"
            f"Duration: {report.duration_minutes:.0f} minutes\n\n"
            f"Improve this executive summary (2-3 sentences, blameless, factual):\n"
            f"{report.executive_summary}\n\n"
            f"Reply with only the improved executive summary text."
        )
        enriched = call_llm(prompt, max_tokens=200)
        if enriched and len(enriched) > 20:
            report.executive_summary = enriched.strip()
    except Exception as exc:
        logger.debug("LLM enrichment skipped: %s", exc)
