"""ITSM change window correlator for SentinalAI major incident analysis.

When a P1/P2 incident fires, an ITSM engineer's first question is:
  "What changed in the past 2 hours that could have caused this?"

This module answers that by correlating:
  - The incident start time
  - A list of ITSM change records (from ServiceNow/Jira/etc.)
  - A list of git commits (from git_worker)

to identify which changes fall within the "change window" preceding the incident
and rank them by causal proximity.

Why this matters for major incidents
-------------------------------------
- In a P1 bridge call, the team needs the most likely causal change in <5 minutes.
- Manual grepping through ServiceNow + GitHub during an incident is slow.
- This module automates the "change window" query so the SRE agent can present:
  "Change record CHG-4521 deployed payment-service v2.1.0 at 14:02 UTC,
   12 minutes before the incident started at 14:14 UTC. Confidence: HIGH."

Algorithm
---------
1. Parse the incident start timestamp.
2. Filter change records to those in [incident_start - window, incident_start].
3. Score each change by:
   - Time proximity: 1.0 if deployed within 30 min, 0.5 if 31-60 min, 0.25 if 61-120 min
   - Service match: +0.4 if same service as incident
   - Risk level: HIGH change = +0.2, NORMAL = 0, LOW = -0.1
   - Type: "deploy"/"emergency" = +0.2, "standard" = 0
4. Correlate with git commits by SHA if available.
5. Return ranked list with correlation metadata.

Configuration
-------------
  CHANGE_WINDOW_MINUTES  — How far back to look (default: 120)
  ITSM_CORRELATION_ENABLED — on/off (default: true)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("sentinalai.itsm_change_correlator")

CHANGE_WINDOW_MINUTES = int(os.environ.get("CHANGE_WINDOW_MINUTES", "120"))
ITSM_CORRELATION_ENABLED = os.environ.get(
    "ITSM_CORRELATION_ENABLED", "true"
).lower() in ("1", "true", "yes")

# Risk level weights
_RISK_SCORES: dict[str, float] = {
    "high":      0.20,
    "critical":  0.25,
    "emergency": 0.25,
    "normal":    0.00,
    "low":      -0.10,
    "standard":  0.00,
}

# Change type weights
_TYPE_SCORES: dict[str, float] = {
    "deploy":       0.20,
    "deployment":   0.20,
    "release":      0.15,
    "emergency":    0.20,
    "hotfix":       0.20,
    "config":       0.10,
    "configuration": 0.10,
    "rollback":     0.15,
    "patch":        0.10,
    "standard":     0.00,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def correlate_change_window(
    incident_time: str,
    change_records: list[dict],
    service: str = "",
    window_minutes: int | None = None,
    git_commits: list[dict] | None = None,
) -> list[dict]:
    """Find ITSM change records that likely caused the incident.

    Args:
        incident_time:   ISO 8601 timestamp when the incident started.
        change_records:  List of change record dicts from itsm_worker. Each
                         record should have keys: id, title, change_type,
                         risk_level, start_time (or scheduled_start_time),
                         service/ci (affected CI/service), commit_sha (optional).
        service:         Affected service name to boost service-matching changes.
        window_minutes:  Override CHANGE_WINDOW_MINUTES.
        git_commits:     Optional list of recent git commits to correlate by SHA.

    Returns:
        List of change record dicts enhanced with:
          - correlation_score (0.0–1.0)
          - minutes_before_incident (int)
          - correlation_reason (str explanation)
          - matched_commit (dict | None — if commit SHA found in git_commits)
        Sorted by correlation_score descending. Empty list if disabled or error.
    """
    if not ITSM_CORRELATION_ENABLED:
        return []

    window = window_minutes if window_minutes is not None else CHANGE_WINDOW_MINUTES

    try:
        incident_dt = _parse_iso(incident_time)
        if incident_dt is None:
            logger.warning("ITSM correlator: could not parse incident_time=%s", incident_time)
            return []
    except Exception as exc:
        logger.warning("ITSM correlator: incident_time parse error: %s", exc)
        return []

    window_start = incident_dt - timedelta(minutes=window)

    # Build commit SHA index for O(1) lookup
    commit_index: dict[str, dict] = {}
    if git_commits:
        for commit in git_commits:
            sha = commit.get("sha", "")
            if sha:
                commit_index[sha] = commit
                # Also index short SHA (first 7 chars)
                if len(sha) >= 7:
                    commit_index[sha[:7]] = commit

    results: list[dict] = []
    for record in change_records:
        scored = _score_change(record, incident_dt, window_start, service, commit_index)
        if scored is not None:
            results.append(scored)

    results.sort(key=lambda x: -x["correlation_score"])
    logger.info(
        "ITSM change window correlation: %d/%d changes in window, top_score=%.2f",
        len(results), len(change_records),
        results[0]["correlation_score"] if results else 0.0,
    )
    return results


def get_most_likely_change(
    incident_time: str,
    change_records: list[dict],
    service: str = "",
    min_score: float = 0.40,
    git_commits: list[dict] | None = None,
) -> dict | None:
    """Return the single most likely causal change, or None if confidence too low.

    Args:
        incident_time:  ISO 8601 incident start time.
        change_records: ITSM change records.
        service:        Affected service.
        min_score:      Minimum correlation_score to return a result (default 0.40).
        git_commits:    Optional git commits for SHA correlation.

    Returns:
        The highest-scoring change record dict with correlation metadata,
        or None if the best score is below min_score.
    """
    ranked = correlate_change_window(incident_time, change_records, service, git_commits=git_commits)
    if not ranked:
        return None
    top = ranked[0]
    if top["correlation_score"] >= min_score:
        return top
    logger.debug(
        "No change meets min_score=%.2f (best=%.2f)",
        min_score, top["correlation_score"],
    )
    return None


def summarise_change_impact(change: dict) -> str:
    """Return a one-line human-readable summary for bridge call / RCA report.

    Example:
        "CHG-4521 'Deploy payment-service v2.1.0' (HIGH risk deploy) deployed
         12 minutes before incident — correlation HIGH (0.82)"
    """
    cid = change.get("id", change.get("number", "UNKNOWN"))
    title = change.get("title", change.get("summary", "untitled"))[:80]
    risk = change.get("risk_level", "?").upper()
    ctype = change.get("change_type", "change").lower()
    mins = change.get("minutes_before_incident", "?")
    score = change.get("correlation_score", 0.0)
    confidence = "HIGH" if score >= 0.70 else "MEDIUM" if score >= 0.45 else "LOW"

    return (
        f"{cid} '{title}' ({risk} risk {ctype}) deployed {mins} min before incident"
        f" — correlation {confidence} ({score:.2f})"
    )


def format_change_window_report(
    correlations: list[dict],
    incident_time: str,
) -> str:
    """Return a multi-line report suitable for an RCA or bridge call runbook."""
    if not correlations:
        return "No ITSM changes found in the change window."

    lines = [f"Change Window Analysis — {len(correlations)} change(s) before incident:"]
    lines.append(f"Incident start: {incident_time}")
    lines.append("")

    for i, ch in enumerate(correlations[:5], 1):
        lines.append(f"{i}. {summarise_change_impact(ch)}")
        reason = ch.get("correlation_reason", "")
        if reason:
            lines.append(f"   Reason: {reason}")
        commit = ch.get("matched_commit")
        if commit:
            sha = commit.get("sha", "")[:12]
            msg = commit.get("message", "")[:60]
            lines.append(f"   Commit: {sha} — {msg}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _score_change(
    record: dict,
    incident_dt: datetime,
    window_start: datetime,
    service: str,
    commit_index: dict[str, dict],
) -> dict | None:
    """Score a single change record. Returns None if outside change window."""
    # Try multiple timestamp fields
    change_time_str = (
        record.get("start_time")
        or record.get("scheduled_start_time")
        or record.get("actual_start_time")
        or record.get("deployed_at")
        or record.get("created_at")
        or ""
    )
    change_dt = _parse_iso(change_time_str)
    if change_dt is None:
        return None  # Can't score without a timestamp

    # Must be within window
    if not (window_start <= change_dt <= incident_dt):
        return None

    minutes_before = max(0, int((incident_dt - change_dt).total_seconds() / 60))

    # Time proximity score
    if minutes_before <= 30:
        time_score = 1.0
    elif minutes_before <= 60:
        time_score = 0.70
    elif minutes_before <= 90:
        time_score = 0.45
    else:
        time_score = 0.25

    # Service match
    change_service = (
        record.get("service")
        or record.get("ci")
        or record.get("configuration_item")
        or ""
    ).lower()
    service_score = 0.0
    if service and change_service:
        if service.lower() == change_service:
            service_score = 0.40
        elif service.lower() in change_service or change_service in service.lower():
            service_score = 0.20

    # Risk level
    risk = record.get("risk_level", record.get("risk", "")).lower()
    risk_score = _RISK_SCORES.get(risk, 0.0)

    # Change type
    ctype = record.get("change_type", record.get("type", "")).lower()
    type_score = 0.0
    for keyword, weight in _TYPE_SCORES.items():
        if keyword in ctype:
            type_score = weight
            break

    # Composite score (time-proximity is dominant)
    raw_score = (time_score * 0.50 + service_score * 0.25 +
                 risk_score * 0.15 + type_score * 0.10)
    correlation_score = round(min(1.0, max(0.0, raw_score)), 3)

    # Build reason string
    reasons = [f"{minutes_before} min before incident"]
    if service_score > 0:
        reasons.append(f"service match ({change_service})")
    if risk_score > 0:
        reasons.append(f"{risk} risk")
    if type_score > 0:
        reasons.append(f"{ctype} type")

    # Commit correlation
    matched_commit: dict | None = None
    commit_sha = record.get("commit_sha", record.get("git_sha", ""))
    if commit_sha and commit_index:
        matched_commit = commit_index.get(commit_sha) or commit_index.get(commit_sha[:7])
        if matched_commit:
            reasons.append("commit matched in git log")
            correlation_score = min(1.0, correlation_score + 0.10)

    return {
        **record,
        "correlation_score":       correlation_score,
        "minutes_before_incident": minutes_before,
        "correlation_reason":      "; ".join(reasons),
        "matched_commit":          matched_commit,
    }


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a timezone-aware datetime."""
    if not ts:
        return None
    try:
        # Handle Z suffix
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
