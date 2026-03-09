"""Structured RCA Report Generator for SentinalAI.

Transforms raw investigation results into a structured, stakeholder-ready
report format. Supports JSON schema output and markdown rendering.

Usage:
    from supervisor.rca_report import generate_rca_report, render_markdown

    report = generate_rca_report(result, incident, receipts, severity)
    markdown = render_markdown(report)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.rca_report")


@dataclass
class RCAReport:
    """Structured Root Cause Analysis report."""

    # Header
    report_id: str = ""
    generated_at: str = ""
    agent_version: str = "0.1.0"

    # Incident summary
    incident_id: str = ""
    incident_type: str = ""
    affected_service: str = ""
    severity_level: int = 3
    severity_label: str = "medium"
    incident_summary: str = ""

    # Root cause determination
    root_cause: str = ""
    confidence: int = 0
    confidence_bracket: str = ""
    reasoning: str = ""

    # Timeline
    evidence_timeline: list[dict] = field(default_factory=list)

    # Evidence summary
    evidence_sources: int = 0
    tool_calls_made: int = 0
    budget_remaining: int = 0
    evidence_completeness: dict = field(default_factory=dict)

    # Hypothesis analysis
    hypothesis_count: int = 0
    winner_hypothesis: str = ""
    historical_matches: list[dict] = field(default_factory=list)
    retrieval_confidence_boost: float = 0.0

    # Investigation steps (from receipts)
    investigation_steps: list[dict] = field(default_factory=list)

    # Remediation
    remediation: dict = field(default_factory=dict)

    # Metadata
    elapsed_ms: float = 0.0
    llm_usage: dict = field(default_factory=dict)
    judge_scores: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a structured dict."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        """Serialize to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent, default=str)


def generate_rca_report(
    result: dict[str, Any],
    incident_id: str = "",
    incident_type: str = "",
    service: str = "",
    severity_level: int = 3,
    severity_label: str = "medium",
    summary: str = "",
    receipts_list: list[dict] | None = None,
    budget_remaining: int = 0,
    elapsed_ms: float = 0.0,
    llm_usage: dict | None = None,
    judge_scores: dict | None = None,
) -> RCAReport:
    """Generate a structured RCA report from investigation results.

    Args:
        result: Raw investigation result dict from agent.investigate()
        incident_id: Incident identifier
        incident_type: Classified incident type
        service: Affected service name
        severity_level: Severity level 1-5
        severity_label: Severity label string
        summary: Incident summary text
        receipts_list: Serialized receipts from ReceiptCollector
        budget_remaining: Remaining execution budget
        elapsed_ms: Total investigation time in milliseconds
        llm_usage: LLM usage metrics dict
        judge_scores: LLM-as-judge scores dict

    Returns:
        Structured RCAReport dataclass
    """
    confidence = result.get("confidence", 0)

    # Build evidence completeness summary
    timeline = result.get("evidence_timeline", [])
    sources_in_timeline = set()
    for entry in timeline:
        src = entry.get("source", "")
        if src:
            sources_in_timeline.add(src)

    evidence_completeness = {
        "logs": "logs" in sources_in_timeline or "log_summary" in sources_in_timeline,
        "golden_signals": "golden_signals" in sources_in_timeline,
        "metrics": "metrics" in sources_in_timeline,
        "events": "events" in sources_in_timeline,
        "changes": "changes" in sources_in_timeline or "itsm_changes" in sources_in_timeline,
    }

    # Build investigation steps from receipts
    steps = []
    if receipts_list:
        for r in receipts_list:
            steps.append({
                "tool": r.get("tool", ""),
                "action": r.get("action", ""),
                "status": r.get("status", ""),
                "elapsed_ms": r.get("elapsed_ms", 0),
                "result_count": r.get("result_count", 0),
                "error": r.get("error", ""),
            })

    return RCAReport(
        report_id=f"rca-{incident_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        generated_at=datetime.now(timezone.utc).isoformat(),
        incident_id=incident_id or result.get("incident_id", ""),
        incident_type=incident_type,
        affected_service=service,
        severity_level=severity_level,
        severity_label=severity_label,
        incident_summary=summary,
        root_cause=result.get("root_cause", ""),
        confidence=confidence,
        confidence_bracket=_confidence_bracket(confidence),
        reasoning=result.get("reasoning", ""),
        evidence_timeline=timeline,
        evidence_sources=len(sources_in_timeline),
        tool_calls_made=len(steps),
        budget_remaining=budget_remaining,
        evidence_completeness=evidence_completeness,
        hypothesis_count=result.get("hypothesis_count", result.get("_hypothesis_count", 0)),
        winner_hypothesis=result.get("winner_hypothesis", result.get("_winner_hypothesis", "")),
        historical_matches=result.get("historical_matches", []),
        retrieval_confidence_boost=result.get("retrieval_confidence_boost", 0.0),
        investigation_steps=steps,
        remediation=result.get("remediation", {}),
        elapsed_ms=elapsed_ms,
        llm_usage=llm_usage or {},
        judge_scores=judge_scores or {},
    )


def render_markdown(report: RCAReport) -> str:
    """Render an RCA report as stakeholder-ready markdown.

    Returns:
        Formatted markdown string suitable for ITSM ticket attachment.
    """
    lines = [
        "# Root Cause Analysis Report",
        "",
        f"**Report ID:** {report.report_id}",
        f"**Generated:** {report.generated_at}",
        "",
        "---",
        "",
        "## Incident Summary",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Incident ID | {report.incident_id} |",
        f"| Type | {report.incident_type} |",
        f"| Service | {report.affected_service} |",
        f"| Severity | {report.severity_label} (level {report.severity_level}) |",
        f"| Summary | {report.incident_summary} |",
        "",
        "---",
        "",
        "## Root Cause",
        "",
        f"**Determination:** {report.root_cause}",
        "",
        f"**Confidence:** {report.confidence}% ({report.confidence_bracket})",
        "",
        "### Reasoning",
        "",
        f"{report.reasoning}",
        "",
        "---",
        "",
        "## Evidence Timeline",
        "",
    ]

    if report.evidence_timeline:
        lines.append("| Timestamp | Source | Service | Event |")
        lines.append("|-----------|--------|---------|-------|")
        for entry in report.evidence_timeline:
            ts = entry.get("timestamp", "N/A")
            src = entry.get("source", "N/A")
            svc = entry.get("service", "N/A")
            evt = entry.get("event", "N/A")
            # Truncate long events for table readability
            if len(evt) > 120:
                evt = evt[:117] + "..."
            lines.append(f"| {ts} | {src} | {svc} | {evt} |")
    else:
        lines.append("*No timeline entries collected.*")

    lines.extend([
        "",
        "---",
        "",
        "## Investigation Steps",
        "",
    ])

    if report.investigation_steps:
        lines.append("| # | Tool | Action | Status | Duration | Results |")
        lines.append("|---|------|--------|--------|----------|---------|")
        for i, step in enumerate(report.investigation_steps, 1):
            status_icon = "OK" if step["status"] == "success" else step["status"].upper()
            lines.append(
                f"| {i} | {step['tool']} | {step['action']} | "
                f"{status_icon} | {step['elapsed_ms']:.0f}ms | "
                f"{step['result_count']} |"
            )
    else:
        lines.append("*No investigation steps recorded.*")

    lines.extend([
        "",
        "---",
        "",
        "## Evidence Completeness",
        "",
    ])

    for source, available in report.evidence_completeness.items():
        icon = "Available" if available else "Missing"
        lines.append(f"- **{source}:** {icon}")

    # Hypothesis analysis
    lines.extend([
        "",
        "---",
        "",
        "## Hypothesis Analysis",
        "",
        f"- **Hypotheses generated:** {report.hypothesis_count}",
        f"- **Winning hypothesis:** {report.winner_hypothesis}",
        f"- **Knowledge retrieval boost:** +{report.retrieval_confidence_boost:.1f}",
    ])

    if report.historical_matches:
        lines.append(f"- **Historical matches:** {len(report.historical_matches)}")

    # Remediation
    if report.remediation:
        lines.extend([
            "",
            "---",
            "",
            "## Remediation Guidance",
            "",
        ])

        risk = report.remediation.get("risk_level", "unknown")
        lines.append(f"**Risk Level:** {risk}")
        lines.append("")

        if report.remediation.get("verify_before_acting"):
            lines.append("> **WARNING: Verify root cause before acting on remediation.**")
            lines.append("")

        for warning in report.remediation.get("warnings", []):
            lines.append(f"> {warning}")
        lines.append("")

        lines.append("### Immediate Actions")
        for action in report.remediation.get("immediate_actions", []):
            lines.append(f"1. {action}")

        lines.append("")
        lines.append("### Permanent Fix")
        for fix in report.remediation.get("permanent_fix", []):
            lines.append(f"1. {fix}")

        runbook = report.remediation.get("runbook_hint", "")
        if runbook:
            lines.append("")
            lines.append(f"**Runbook:** {runbook}")

    # Metadata
    lines.extend([
        "",
        "---",
        "",
        "## Investigation Metadata",
        "",
        f"- **Total duration:** {report.elapsed_ms:.0f}ms",
        f"- **Tool calls:** {report.tool_calls_made}",
        f"- **Budget remaining:** {report.budget_remaining}",
        f"- **Evidence sources:** {report.evidence_sources}",
        f"- **Agent version:** {report.agent_version}",
    ])

    if report.llm_usage:
        total_tokens = (
            report.llm_usage.get("refine_input_tokens", 0) +
            report.llm_usage.get("refine_output_tokens", 0) +
            report.llm_usage.get("reasoning_input_tokens", 0) +
            report.llm_usage.get("reasoning_output_tokens", 0)
        )
        if total_tokens:
            lines.append(f"- **LLM tokens used:** {total_tokens}")

    if report.judge_scores:
        overall = report.judge_scores.get("overall", 0)
        lines.append(f"- **Quality score (judge):** {overall:.2f}")

    lines.append("")
    return "\n".join(lines)


def _confidence_bracket(confidence: int) -> str:
    """Map confidence score to a human-readable bracket."""
    if confidence <= 25:
        return "very low"
    if confidence <= 50:
        return "low"
    if confidence <= 75:
        return "medium"
    if confidence <= 90:
        return "high"
    return "very high"
