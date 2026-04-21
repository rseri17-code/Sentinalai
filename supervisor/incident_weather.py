"""Incident weather forecast module for SentinalAI.

Generates a forward-looking risk forecast: "Given what's scheduled to change,
what incidents are most likely in the next 24 hours?"

Like a weather forecast for your production system.  Combines:
  - Upcoming ITSM changes
  - Current service health metrics
  - Historical incident patterns from the experience store

Completely novel — no competitor does this.

Risk score construction (capped at 100):
  Base risk from change_type:
    database_migration → 70
    deployment         → 40
    config_change      → 30
    maintenance        → 20

  Boosts:
    + 25  same service had incident within 7 days of a similar change
    + 20  current error_rate > 1%
    + 15  current latency > 2× baseline
    + 10  current cpu > 80%

  RiskLevel thresholds:
    LOW      < 20
    MODERATE 20-50
    HIGH     50-75
    SEVERE   > 75
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any

logger = logging.getLogger("sentinalai.incident_weather")

# ---------------------------------------------------------------------------
# Enums and data classes
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW      = "low"       # < 20% probability
    MODERATE = "moderate"  # 20-50%
    HIGH     = "high"      # 50-75%
    SEVERE   = "severe"    # > 75%


@dataclass
class RiskFactor:
    factor_type: str   # "scheduled_change", "historical_pattern", "current_health", "known_fragility"
    description: str
    weight: float      # 0.0-1.0 contribution to risk score
    evidence: str      # specific data point supporting this factor


@dataclass
class ServiceForecast:
    service: str
    risk_level: RiskLevel
    risk_score: float                       # 0.0-100.0
    predicted_incident_types: list[str]     # e.g. ["error_spike", "latency"]
    risk_window_start: str                  # ISO8601
    risk_window_end: str                    # ISO8601
    risk_factors: list[RiskFactor]
    recommended_preemptive_actions: list[str]
    confidence: float                       # 0.0-1.0


@dataclass
class WeatherForecast:
    generated_at: str
    forecast_horizon_hours: int             # e.g. 24
    forecasts: list[ServiceForecast]        # one per at-risk service
    overall_system_risk: RiskLevel
    headline: str

    def get_highest_risk_services(self, n: int = 3) -> list[ServiceForecast]:
        """Return top N highest-risk services."""
        sorted_forecasts = sorted(
            self.forecasts, key=lambda f: f.risk_score, reverse=True
        )
        return sorted_forecasts[:n]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base risk scores by change type
_CHANGE_TYPE_BASE_RISK: dict[str, float] = {
    "database_migration": 70.0,
    "deployment":         40.0,
    "config_change":      30.0,
    "maintenance":        20.0,
}

# Predicted incident types associated with each change type
_CHANGE_TYPE_INCIDENT_TYPES: dict[str, list[str]] = {
    "database_migration": ["latency", "timeout", "error_spike"],
    "deployment":         ["error_spike", "latency"],
    "config_change":      ["error_spike", "silent_failure"],
    "maintenance":        ["timeout", "missing_data"],
}

# Historical pattern boost: service had incident within 7 days of similar change
_HISTORICAL_BOOST = 25.0

# Current health boosts
_HEALTH_ERROR_RATE_BOOST  = 20.0  # error_rate > 1%
_HEALTH_LATENCY_BOOST     = 15.0  # latency > 2× baseline (we use 200ms as a proxy baseline)
_HEALTH_CPU_BOOST         = 10.0  # cpu > 80%

_LATENCY_BASELINE_MS = 200.0  # default baseline latency in milliseconds

# Preemptive action templates by change type
_PREEMPTIVE_ACTIONS: dict[str, list[str]] = {
    "database_migration": [
        "Ensure rollback plan ready with tested restore procedure",
        "Schedule extra on-call coverage during migration window",
        "Run load test before change window to establish baseline",
        "Pre-warm connection pools before migration starts",
        "Enable enhanced monitoring and alerting 30 minutes before start",
    ],
    "deployment": [
        "Ensure rollback plan ready (previous artifact pinned)",
        "Run canary deployment to 5% traffic before full rollout",
        "Review recent error logs for pre-existing fragility",
        "Enable feature flags for instant rollback if needed",
    ],
    "config_change": [
        "Validate config change in staging environment first",
        "Ensure rollback plan ready with previous config snapshot",
        "Monitor error rate closely for 30 minutes post-change",
        "Schedule extra on-call coverage during change window",
    ],
    "maintenance": [
        "Notify dependent service teams of maintenance window",
        "Ensure rollback plan ready",
        "Monitor dependent services for cascading impact",
        "Confirm graceful drain of in-flight requests before start",
    ],
}

_DEFAULT_PREEMPTIVE_ACTIONS = [
    "Ensure rollback plan ready",
    "Schedule extra on-call coverage",
    "Run load test before change window",
]

# RiskLevel thresholds
_RISK_LEVEL_THRESHOLDS = [
    (75.0, RiskLevel.SEVERE),
    (50.0, RiskLevel.HIGH),
    (20.0, RiskLevel.MODERATE),
    (0.0,  RiskLevel.LOW),
]


def _score_to_risk_level(score: float) -> RiskLevel:
    """Map a numeric risk score (0-100) to a RiskLevel."""
    for threshold, level in _RISK_LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return RiskLevel.LOW


def _overall_risk_level(forecasts: list[ServiceForecast]) -> RiskLevel:
    """Return the worst risk level across all service forecasts."""
    if not forecasts:
        return RiskLevel.LOW
    level_order = {
        RiskLevel.LOW:      0,
        RiskLevel.MODERATE: 1,
        RiskLevel.HIGH:     2,
        RiskLevel.SEVERE:   3,
    }
    return max(forecasts, key=lambda f: level_order[f.risk_level]).risk_level


# ---------------------------------------------------------------------------
# Historical pattern analysis
# ---------------------------------------------------------------------------

def _had_recent_incident(
    service: str,
    change_type: str,
    historical_experiences: list[dict],
    lookback_days: int = 7,
) -> tuple[bool, str]:
    """Check if *service* had an incident within *lookback_days* of a similar change.

    Returns (matched, evidence_description).
    """
    # Derive expected incident types for this change_type
    expected_types = _CHANGE_TYPE_INCIDENT_TYPES.get(change_type, [])
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    matching_experiences = []
    for exp in historical_experiences:
        exp_service = exp.get("service", "")
        exp_type    = exp.get("incident_type", "")
        exp_ts_raw  = exp.get("timestamp", "")

        # Service match (exact or prefix)
        service_matches = (
            exp_service == service
            or (service and exp_service.startswith(service.split("-")[0]))
        )
        if not service_matches:
            continue

        # Type match
        type_matches = not expected_types or exp_type in expected_types
        if not type_matches:
            continue

        # Recency check
        if exp_ts_raw:
            try:
                exp_ts = datetime.fromisoformat(exp_ts_raw.replace("Z", "+00:00"))
                if exp_ts < cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # malformed timestamp — include it (conservative)

        matching_experiences.append(exp)

    if matching_experiences:
        sample = matching_experiences[0]
        evidence = (
            f"Service '{service}' had a {sample.get('incident_type', 'unknown')} incident "
            f"on {sample.get('timestamp', 'unknown date')} "
            f"(root cause: {sample.get('root_cause', 'unknown')[:80]})"
        )
        return True, evidence

    return False, ""


# ---------------------------------------------------------------------------
# Headline generation
# ---------------------------------------------------------------------------

def _generate_headline(
    forecasts: list[ServiceForecast],
    upcoming_changes: list[dict],
) -> str:
    """Summarise the most concerning risk combination into a single headline."""
    if not forecasts:
        return "No significant risk factors detected in forecast window"

    # Sort descending by risk score
    top = sorted(forecasts, key=lambda f: f.risk_score, reverse=True)
    highest = top[0]

    # Count high/severe services
    severe_count = sum(1 for f in forecasts if f.risk_level in (RiskLevel.SEVERE, RiskLevel.HIGH))
    migration_count = sum(
        1 for c in upcoming_changes if c.get("change_type") == "database_migration"
    )

    parts: list[str] = []

    if highest.risk_level == RiskLevel.SEVERE:
        parts.append(f"SEVERE risk: {highest.service} at {highest.risk_score:.0f}/100")
    elif highest.risk_level == RiskLevel.HIGH:
        parts.append(f"High risk: {highest.service} at {highest.risk_score:.0f}/100")
    else:
        parts.append(f"Elevated risk: {highest.service} at {highest.risk_score:.0f}/100")

    if migration_count:
        parts.append(f"{migration_count} DB migration(s) scheduled")

    if severe_count > 1:
        parts.append(f"{severe_count} services in high/severe risk")

    # Highlight fragile services (current health issues)
    fragile = [
        f.service for f in forecasts
        if any(
            rf.factor_type == "current_health" for rf in f.risk_factors
        )
    ]
    if fragile:
        labels = ", ".join(fragile[:3])
        parts.append(f"{labels} showing current health issues")

    return " — ".join(parts)


# ---------------------------------------------------------------------------
# Main forecast function
# ---------------------------------------------------------------------------

def generate_forecast(
    upcoming_changes: list[dict],    # [{service, change_type, scheduled_at, risk}, ...]
    current_health: dict[str, Any],  # {service: {error_rate, latency_p95, cpu, memory}}
    historical_experiences: list[dict],  # from experience store
    forecast_hours: int = 24,
) -> WeatherForecast:
    """Generate a risk forecast for the next *forecast_hours* hours.

    For each service in *upcoming_changes*, compute a risk score by combining:
      - Base risk from change_type
      - Historical pattern boost (same service, similar change, within 7 days)
      - Current health boost (error_rate, latency, cpu)

    Args:
        upcoming_changes:      List of ITSM change records.
        current_health:        Current metric snapshot per service.
        historical_experiences: Past incidents from experience store.
        forecast_hours:        Look-ahead window in hours.

    Returns:
        WeatherForecast with per-service forecasts.
    """
    now = datetime.now(timezone.utc)
    forecast_end = now + timedelta(hours=forecast_hours)

    forecasts: list[ServiceForecast] = []

    # Group changes by service so we produce one ServiceForecast per service
    services_seen: set[str] = set()
    changes_by_service: dict[str, list[dict]] = {}
    for change in upcoming_changes:
        svc = change.get("service", "unknown")
        changes_by_service.setdefault(svc, []).append(change)

    for service, changes in changes_by_service.items():
        services_seen.add(service)

        risk_score = 0.0
        risk_factors: list[RiskFactor] = []
        predicted_types: list[str] = []

        for change in changes:
            change_type = change.get("change_type", "deployment")
            scheduled_at = change.get("scheduled_at", now.isoformat())

            # --- Base risk from change type ---
            base = _CHANGE_TYPE_BASE_RISK.get(change_type, 30.0)
            risk_score += base
            risk_factors.append(RiskFactor(
                factor_type="scheduled_change",
                description=f"{change_type} scheduled for {service}",
                weight=round(base / 100.0, 2),
                evidence=f"Scheduled at {scheduled_at} (change type: {change_type})",
            ))

            # Predicted incident types
            for t in _CHANGE_TYPE_INCIDENT_TYPES.get(change_type, ["error_spike"]):
                if t not in predicted_types:
                    predicted_types.append(t)

            # --- Historical pattern boost ---
            had_incident, hist_evidence = _had_recent_incident(
                service, change_type, historical_experiences
            )
            if had_incident:
                risk_score += _HISTORICAL_BOOST
                risk_factors.append(RiskFactor(
                    factor_type="historical_pattern",
                    description=(
                        f"{service} had an incident within 7 days of a similar "
                        f"{change_type} previously"
                    ),
                    weight=round(_HISTORICAL_BOOST / 100.0, 2),
                    evidence=hist_evidence,
                ))

        # --- Current health boosts ---
        health = current_health.get(service, {})
        error_rate = float(health.get("error_rate", 0.0))
        latency_p95 = float(health.get("latency_p95", 0.0))
        cpu = float(health.get("cpu", 0.0))

        if error_rate > 0.01:  # > 1%
            risk_score += _HEALTH_ERROR_RATE_BOOST
            risk_factors.append(RiskFactor(
                factor_type="current_health",
                description=f"{service} error rate elevated at {error_rate:.2%}",
                weight=round(_HEALTH_ERROR_RATE_BOOST / 100.0, 2),
                evidence=f"Current error_rate={error_rate:.4f} (threshold: 1%)",
            ))
            if "error_spike" not in predicted_types:
                predicted_types.append("error_spike")

        if latency_p95 > _LATENCY_BASELINE_MS * 2:
            risk_score += _HEALTH_LATENCY_BOOST
            risk_factors.append(RiskFactor(
                factor_type="current_health",
                description=(
                    f"{service} latency p95 is {latency_p95:.0f}ms "
                    f"(>{_LATENCY_BASELINE_MS * 2:.0f}ms baseline)"
                ),
                weight=round(_HEALTH_LATENCY_BOOST / 100.0, 2),
                evidence=f"Current latency_p95={latency_p95:.1f}ms (baseline: {_LATENCY_BASELINE_MS}ms)",
            ))
            if "latency" not in predicted_types:
                predicted_types.append("latency")

        if cpu > 80.0:
            risk_score += _HEALTH_CPU_BOOST
            risk_factors.append(RiskFactor(
                factor_type="current_health",
                description=f"{service} CPU utilization high at {cpu:.1f}%",
                weight=round(_HEALTH_CPU_BOOST / 100.0, 2),
                evidence=f"Current cpu={cpu:.1f}% (threshold: 80%)",
            ))
            if "saturation" not in predicted_types:
                predicted_types.append("saturation")

        # Cap at 100
        risk_score = min(100.0, risk_score)

        risk_level = _score_to_risk_level(risk_score)

        # Preemptive actions: aggregate from all change types in this service
        actions: list[str] = []
        seen_actions: set[str] = set()
        for change in changes:
            ct = change.get("change_type", "deployment")
            for action in _PREEMPTIVE_ACTIONS.get(ct, _DEFAULT_PREEMPTIVE_ACTIONS):
                if action not in seen_actions:
                    actions.append(action)
                    seen_actions.add(action)

        # Confidence based on data completeness:
        # historical_experiences present → higher confidence
        has_history = bool(historical_experiences)
        has_health  = bool(health)
        confidence_score = 0.5  # base
        if has_history:
            confidence_score += 0.3
        if has_health:
            confidence_score += 0.2
        confidence = round(min(1.0, confidence_score), 2)

        forecast = ServiceForecast(
            service=service,
            risk_level=risk_level,
            risk_score=round(risk_score, 2),
            predicted_incident_types=predicted_types,
            risk_window_start=now.isoformat(),
            risk_window_end=forecast_end.isoformat(),
            risk_factors=risk_factors,
            recommended_preemptive_actions=actions,
            confidence=confidence,
        )
        forecasts.append(forecast)
        logger.info(
            "ServiceForecast: service=%s risk_level=%s score=%.1f confidence=%.2f",
            service, risk_level.value, risk_score, confidence,
        )

    # Sort forecasts: highest risk first
    forecasts.sort(key=lambda f: f.risk_score, reverse=True)

    overall_risk = _overall_risk_level(forecasts)
    headline = _generate_headline(forecasts, upcoming_changes)

    return WeatherForecast(
        generated_at=now.isoformat(),
        forecast_horizon_hours=forecast_hours,
        forecasts=forecasts,
        overall_system_risk=overall_risk,
        headline=headline,
    )
