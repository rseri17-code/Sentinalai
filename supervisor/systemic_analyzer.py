"""Systemic Analyzer: cross-incident pattern detection and architectural anti-pattern reporting.

Looks across 90 days of incidents, identifies recurring (service, root-cause)
clusters, and generates opinionated architectural recommendations.  The SRE
insight: if the same service has 15 incidents with the same root-cause
category, that is not an operational problem — it is a DESIGN problem.

Usage:
    from supervisor.systemic_analyzer import extract_anti_patterns

    report = extract_anti_patterns(experiences, window_days=90)
    for ap in report.anti_patterns:
        print(ap.architectural_recommendation)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger("sentinalai.systemic_analyzer")


# ---------------------------------------------------------------------------
# Root-cause category keyword map  (ordered: first match wins)
# ---------------------------------------------------------------------------

_RC_KEYWORDS: list[tuple[str, str]] = [
    ("connection pool",   "connection_pool_exhaustion"),
    ("connection_pool",   "connection_pool_exhaustion"),
    ("pool exhaustion",   "connection_pool_exhaustion"),
    ("memory leak",       "memory_pressure"),
    ("memory",            "memory_pressure"),
    ("oom",               "memory_pressure"),
    ("out of memory",     "memory_pressure"),
    ("deploy",            "deployment_regression"),
    ("rollout",           "deployment_regression"),
    ("release",           "deployment_regression"),
    ("timeout",           "timeout_cascade"),
    ("timed out",         "timeout_cascade"),
    ("circuit",           "timeout_cascade"),
    ("disk",              "disk_saturation"),
    ("storage",           "disk_saturation"),
    ("iops",              "disk_saturation"),
    ("network",           "network_instability"),
    ("packet loss",       "network_instability"),
    ("latency spike",     "network_instability"),
    ("certificate",       "certificate_management"),
    ("cert expir",        "certificate_management"),
    ("tls",               "certificate_management"),
    ("ssl",               "certificate_management"),
]

# Anti-pattern architectural recommendations by root-cause category
_RECOMMENDATIONS: dict[str, dict[str, Any]] = {
    "connection_pool_exhaustion": {
        "recommendation": (
            "Implement async connection pooling with PgBouncer (transaction mode) and add "
            "a circuit breaker (e.g. resilience4j) on the DB call path. "
            "Set explicit pool-size limits aligned to DB max_connections."
        ),
        "actions": [
            "Deploy PgBouncer in transaction-pooling mode between app and database.",
            "Add circuit breaker with 50 % failure-rate threshold and 5 s open window.",
            "Configure explicit connection-pool size per service (CPU_count × 2 + 1).",
            "Emit pool-utilisation metrics and alert at 70 % saturation.",
            "Audit all synchronous DB calls in async hot paths and convert to async.",
        ],
        "prevention_rate": 0.85,
    },
    "memory_pressure": {
        "recommendation": (
            "Introduce memory-limit resource quotas in Kubernetes (requests = limits), "
            "add heap-profiling on OOMKill events, and instrument GC pause metrics. "
            "Consider a sidecar memory-pressure exporter and VPA auto-rightsizing."
        ),
        "actions": [
            "Set Kubernetes memory requests == limits for every workload.",
            "Enable OOMKill event alerts with automatic heap-dump capture.",
            "Integrate continuous heap profiling (e.g. Pyroscope / async-profiler).",
            "Deploy VPA in Recommendation mode; review weekly and apply limits.",
            "Add integration tests with memory-bound scenarios in CI.",
        ],
        "prevention_rate": 0.75,
    },
    "deployment_regression": {
        "recommendation": (
            "Add canary deployment gates with automated rollback: deploy to 5 % of traffic, "
            "hold for 15 min while comparing error-rate and latency against baseline, "
            "and auto-promote or auto-rollback based on SLO delta."
        ),
        "actions": [
            "Implement canary analysis (e.g. Argo Rollouts + Prometheus metrics).",
            "Gate promotions on error-rate delta < 0.1 % and p95 delta < 20 %.",
            "Require automated integration smoke-tests to pass before canary promotion.",
            "Add deployment freeze windows for peak-traffic periods.",
            "Correlate deploys with incident timeline in dashboards automatically.",
        ],
        "prevention_rate": 0.80,
    },
    "timeout_cascade": {
        "recommendation": (
            "Implement hierarchical timeouts with bulkhead isolation: each service sets "
            "call-level timeouts < upstream deadline, and uses separate thread/connection "
            "pools per downstream dependency to prevent cascade failures."
        ),
        "actions": [
            "Define and enforce per-call timeout budgets across all service dependencies.",
            "Add bulkhead isolation (separate pools) per downstream service.",
            "Implement retry with exponential back-off + jitter (max 3 retries).",
            "Add fallback responses for non-critical downstream calls.",
            "Instrument and alert on deadline-exceeded errors at each service boundary.",
        ],
        "prevention_rate": 0.70,
    },
    "disk_saturation": {
        "recommendation": (
            "Add disk-utilisation alerts at 70 %/85 %/95 % thresholds, implement "
            "automated log rotation and retention policies, and use persistent volume "
            "claims with dynamic provisioning so capacity can be expanded without "
            "redeployment."
        ),
        "actions": [
            "Alert on disk utilisation > 70 % with 15-min sustained window.",
            "Implement log-rotation with maximum 7-day retention in production.",
            "Use dynamic PVC provisioning with auto-resize (e.g. volume-resize admission webhook).",
            "Move ephemeral write workloads to tmpfs or object storage.",
            "Schedule weekly disk-usage audits and clean up stale artefacts.",
        ],
        "prevention_rate": 0.90,
    },
    "network_instability": {
        "recommendation": (
            "Add network-level retries with exponential back-off at the service mesh "
            "(Istio/Linkerd), enable mTLS between all services, and instrument "
            "per-connection error rates with automatic node-drain on sustained packet loss."
        ),
        "actions": [
            "Configure service-mesh retries (max 3, 500 ms back-off) for idempotent calls.",
            "Enable mTLS and network policy enforcement across all namespaces.",
            "Add per-node network-error rate dashboards; drain node if > 1 % loss.",
            "Implement health-check endpoints and remove unhealthy pods from LB immediately.",
            "Use TCP keep-alives and set appropriate socket timeouts on all connections.",
        ],
        "prevention_rate": 0.65,
    },
    "certificate_management": {
        "recommendation": (
            "Automate certificate lifecycle with cert-manager (Let's Encrypt or internal CA), "
            "alert 30 days before expiry, and enforce certificate rotation tests in staging "
            "every 60 days."
        ),
        "actions": [
            "Deploy cert-manager with automated renewal (renew at 30 days before expiry).",
            "Alert at 30 days, 14 days, and 7 days before certificate expiry.",
            "Store certificate inventory in CMDB and reconcile weekly.",
            "Run certificate rotation drill in staging every 60 days.",
            "Block deployments if any dependency certificate expires within 14 days.",
        ],
        "prevention_rate": 0.95,
    },
    "unknown": {
        "recommendation": (
            "Improve observability: add structured logging with correlation IDs, "
            "distributed tracing (OpenTelemetry), and define explicit SLIs/SLOs "
            "so root causes can be identified earlier and more precisely."
        ),
        "actions": [
            "Add OpenTelemetry instrumentation for traces, metrics, and logs.",
            "Define SLIs (error rate, p95 latency) and SLOs (99.5 % availability).",
            "Create runbooks for the top 5 incident types and link from alerts.",
            "Conduct a blameless post-mortem after every incident and track action items.",
            "Review alert coverage against past incidents to close detection gaps.",
        ],
        "prevention_rate": 0.40,
    },
}

# Priority thresholds (incidents per week)
_PRIORITY_URGENT = 2.0
_PRIORITY_HIGH   = 1.0

# Weight for anti-pattern severity in systemic_risk_score computation.
# Higher-frequency patterns with more downtime get heavier penalties.
_MAX_RISK_CONTRIBUTION = 20.0  # per anti-pattern, capped


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AntiPattern:
    """A recurring incident cluster that indicates an architectural problem."""

    pattern_id: str
    service: str                      # affected service, or "*" for global
    incident_type: str                # dominant incident type in this cluster
    root_cause_category: str          # normalised category string
    incident_count: int
    first_seen: str                   # ISO-8601
    last_seen: str                    # ISO-8601
    frequency_per_week: float
    severity_distribution: dict       # {"Critical": n, "High": n, …}
    total_downtime_minutes: int

    pattern_description: str
    root_cause_hypothesis: str

    architectural_recommendation: str
    recommended_actions: list[str]
    estimated_prevention_rate: float  # 0.0–1.0
    priority: str                     # "urgent" | "high" | "medium"


@dataclass
class SystemicAnalysisReport:
    """Top-level report from systemic incident analysis."""

    generated_at: str
    analysis_window_days: int
    total_incidents_analyzed: int
    anti_patterns: list[AntiPattern]
    systemic_risk_score: float        # 0–100 (lower = worse)
    top_recommendation: str
    estimated_incident_reduction_pct: float  # if all anti-patterns fixed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_anti_patterns(
    experiences: list[dict],
    window_days: int = 90,
    min_incident_count: int = 3,
) -> SystemicAnalysisReport:
    """Analyse the experience store to find recurring incident anti-patterns.

    Args:
        experiences:        List of experience dicts from the experience store.
                            Each dict must have at minimum: incident_id,
                            incident_type, service, root_cause, timestamp.
                            Optional: severity, resolution_minutes,
                            online_quality_score.
        window_days:        How many days back to analyse (default 90).
        min_incident_count: Minimum incidents to declare an anti-pattern (default 3).

    Returns:
        SystemicAnalysisReport with all detected anti-patterns and scores.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    # ------------------------------------------------------------------
    # 1. Filter to the analysis window
    # ------------------------------------------------------------------
    windowed: list[dict] = []
    for exp in experiences:
        ts = _parse_timestamp(exp.get("timestamp", ""))
        if ts is None or ts >= cutoff:
            windowed.append(exp)

    total_analyzed = len(windowed)

    # ------------------------------------------------------------------
    # 2. Group by (service, root_cause_category)
    # ------------------------------------------------------------------
    groups: dict[tuple[str, str], list[dict]] = {}
    for exp in windowed:
        service = exp.get("service", "unknown")
        rc      = exp.get("root_cause", "")
        category = _classify_root_cause(rc)
        key = (service, category)
        groups.setdefault(key, []).append(exp)

    # ------------------------------------------------------------------
    # 3. Build AntiPattern for groups above the threshold
    # ------------------------------------------------------------------
    anti_patterns: list[AntiPattern] = []
    for (service, category), group in groups.items():
        if len(group) < min_incident_count:
            continue

        ap = _build_anti_pattern(
            service=service,
            category=category,
            group=group,
            window_days=window_days,
            now=now,
        )
        anti_patterns.append(ap)

    # Sort by frequency (highest first) so the most painful patterns surface first
    anti_patterns.sort(key=lambda ap: ap.frequency_per_week, reverse=True)

    # ------------------------------------------------------------------
    # 4. Compute systemic_risk_score and top recommendation
    # ------------------------------------------------------------------
    systemic_risk_score = _compute_risk_score(anti_patterns)

    top_recommendation = (
        anti_patterns[0].architectural_recommendation
        if anti_patterns
        else "No recurring patterns detected — maintain current SLO reviews."
    )

    estimated_reduction = (
        sum(ap.estimated_prevention_rate for ap in anti_patterns) / len(anti_patterns)
        if anti_patterns
        else 0.0
    )

    return SystemicAnalysisReport(
        generated_at=now.isoformat(),
        analysis_window_days=window_days,
        total_incidents_analyzed=total_analyzed,
        anti_patterns=anti_patterns,
        systemic_risk_score=round(systemic_risk_score, 2),
        top_recommendation=top_recommendation,
        estimated_incident_reduction_pct=round(estimated_reduction * 100, 1),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _classify_root_cause(root_cause: str) -> str:
    """Map a root_cause string to a normalised category using keyword matching."""
    rc_lower = (root_cause or "").lower()
    for keyword, category in _RC_KEYWORDS:
        if keyword in rc_lower:
            return category
    return "unknown"


def _build_anti_pattern(
    service: str,
    category: str,
    group: list[dict],
    window_days: int,
    now: datetime,
) -> AntiPattern:
    """Construct an AntiPattern from a cluster of matching incidents."""

    incident_count = len(group)

    # Timestamps
    timestamps = [_parse_timestamp(exp.get("timestamp", "")) for exp in group]
    valid_ts   = [t for t in timestamps if t is not None]
    first_seen = min(valid_ts).isoformat() if valid_ts else now.isoformat()
    last_seen  = max(valid_ts).isoformat() if valid_ts else now.isoformat()

    # Frequency
    actual_window_weeks = window_days / 7.0
    frequency_per_week  = round(incident_count / actual_window_weeks, 3)

    # Severity distribution
    severity_dist: dict[str, int] = {}
    for exp in group:
        sev = _normalise_severity(exp.get("severity", exp.get("severity_label", "")))
        severity_dist[sev] = severity_dist.get(sev, 0) + 1

    # Total downtime
    total_downtime = sum(
        _safe_int(exp, "resolution_minutes", _default_resolution(exp))
        for exp in group
    )

    # Dominant incident type (most common)
    type_counts: dict[str, int] = {}
    for exp in group:
        itype = exp.get("incident_type", "unknown")
        type_counts[itype] = type_counts.get(itype, 0) + 1
    dominant_type = max(type_counts, key=type_counts.__getitem__) if type_counts else "unknown"

    # Priority
    if frequency_per_week >= _PRIORITY_URGENT:
        priority = "urgent"
    elif frequency_per_week >= _PRIORITY_HIGH:
        priority = "high"
    else:
        priority = "medium"

    # Lookup recommendations
    rec_data = _RECOMMENDATIONS.get(category, _RECOMMENDATIONS["unknown"])

    # Human-readable descriptions
    pattern_description = (
        f"{service} has had {incident_count} '{category.replace('_', ' ')}' incidents "
        f"over the past {window_days} days "
        f"({frequency_per_week:.1f}/week, {total_downtime} min total downtime)."
    )
    root_cause_hypothesis = _hypothesis_for(category, service, group)

    # Unique pattern ID
    pattern_id = f"{service}__{category}"

    return AntiPattern(
        pattern_id=pattern_id,
        service=service,
        incident_type=dominant_type,
        root_cause_category=category,
        incident_count=incident_count,
        first_seen=first_seen,
        last_seen=last_seen,
        frequency_per_week=frequency_per_week,
        severity_distribution=severity_dist,
        total_downtime_minutes=total_downtime,
        pattern_description=pattern_description,
        root_cause_hypothesis=root_cause_hypothesis,
        architectural_recommendation=rec_data["recommendation"],
        recommended_actions=list(rec_data["actions"]),
        estimated_prevention_rate=rec_data["prevention_rate"],
        priority=priority,
    )


def _hypothesis_for(category: str, service: str, group: list[dict]) -> str:
    """Generate a concise root-cause hypothesis for the anti-pattern."""
    hypotheses: dict[str, str] = {
        "connection_pool_exhaustion": (
            f"Synchronous database calls in '{service}' exhaust the connection pool under "
            "sustained load. The pool is likely under-sized relative to concurrency, or "
            "connections are not returned promptly (missing finally blocks / context managers)."
        ),
        "memory_pressure": (
            f"'{service}' exhibits a gradual memory leak between restarts. "
            "Likely cause: unbounded caches, growing event listeners, or large in-memory "
            "datasets that are never evicted. OOMKill events mask the leak by restarting "
            "the process before heap analysis is captured."
        ),
        "deployment_regression": (
            f"Deployments to '{service}' repeatedly introduce regressions that are not "
            "caught in staging or canary. Root causes likely include: missing integration "
            "tests, no canary traffic analysis, and insufficient rollback automation."
        ),
        "timeout_cascade": (
            f"'{service}' sits on a critical dependency path. A slow downstream causes "
            "queued requests to accumulate, exhausting threads or connections and cascading "
            "to callers. Per-call timeouts and bulkheads are absent or misconfigured."
        ),
        "disk_saturation": (
            f"'{service}' writes logs, temporary files, or data without adequate rotation "
            "or retention policy. Disk fills predictably at end-of-quarter or during "
            "high-traffic events when verbose logging is enabled."
        ),
        "network_instability": (
            f"'{service}' experiences recurring packet loss or connection resets, "
            "likely due to noisy-neighbour contention on shared network infrastructure, "
            "misconfigured MTU, or kernel TCP buffer exhaustion under bursty load."
        ),
        "certificate_management": (
            f"Certificates for '{service}' are managed manually and expire without "
            "sufficient advance warning. Lack of automated renewal and missing expiry "
            "alerts causes recurring outages at renewal deadlines."
        ),
        "unknown": (
            f"'{service}' has recurring incidents whose root cause cannot be precisely "
            "categorised from available data. Improved observability (structured logging, "
            "distributed tracing) is needed before a specific hypothesis can be confirmed."
        ),
    }
    return hypotheses.get(category, hypotheses["unknown"])


def _compute_risk_score(anti_patterns: list[AntiPattern]) -> float:
    """Compute overall systemic risk score (100 = perfect health, 0 = critical).

    Each anti-pattern contributes a penalty proportional to:
      - frequency_per_week  (more frequent = worse)
      - incident_count      (more incidents = worse)
      - estimated_prevention_rate (higher = more impactful to fix = bigger gap)

    The total penalty is capped so a single catastrophic pattern cannot drive
    the score below 0.
    """
    if not anti_patterns:
        return 100.0

    total_penalty = 0.0
    for ap in anti_patterns:
        # Base penalty: frequency × 5 points/week, capped at max contribution
        freq_penalty = min(ap.frequency_per_week * 5.0, _MAX_RISK_CONTRIBUTION * 0.6)
        # Severity multiplier from incident count
        count_penalty = min(ap.incident_count * 0.5, _MAX_RISK_CONTRIBUTION * 0.4)
        penalty = freq_penalty + count_penalty
        total_penalty += min(penalty, _MAX_RISK_CONTRIBUTION)

    score = 100.0 - total_penalty
    return max(0.0, score)


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string. Returns None on failure."""
    if not ts:
        return None
    try:
        # Python 3.7+ fromisoformat does not handle trailing Z
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _normalise_severity(sev: Any) -> str:
    """Map severity to a standard label string."""
    if isinstance(sev, int):
        return {1: "Critical", 2: "High", 3: "Medium", 4: "Low", 5: "Info"}.get(sev, "Unknown")
    label_map = {
        "critical": "Critical",
        "high":     "High",
        "medium":   "Medium",
        "low":      "Low",
        "info":     "Info",
    }
    return label_map.get(str(sev).lower(), str(sev) or "Unknown")


def _default_resolution(exp: dict) -> int:
    """Fallback resolution estimate based on severity."""
    sev = exp.get("severity", 3)
    try:
        sev_int = int(sev)
    except (TypeError, ValueError):
        sev_int = 3
    return {1: 120, 2: 60, 3: 30, 4: 15, 5: 10}.get(sev_int, 30)


def _safe_int(d: dict, key: str, default: int) -> int:
    try:
        val = d.get(key, default)
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default
