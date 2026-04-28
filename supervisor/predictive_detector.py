"""Predictive incident detector for SentinalAI.

Detects incidents BEFORE they page by monitoring rising signal trends against
SLA thresholds and firing pre-emptive alerts when a breach is imminent.

The SRE dream: "I knew about this 12 minutes before it paged."

Design:
  - analyze_trend: manual linear regression (least squares), no scipy
  - R² quality gate: only flag trends with R² > 0.5 (meaningful, not noise)
  - Urgency tiers:
      BREACHED  — already over threshold
      IMMINENT  — < 15 min to breach at current slope
      WARNING   — 15-30 min to breach
      WATCH     — > 30 min but trending toward threshold
  - Metric-name → incident_type mapping covers memory, cpu, error_rate,
    latency, connection_pool, disk, goroutine patterns
  - Confidence = R² × urgency_weight
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("sentinalai.predictive_detector")

# ---------------------------------------------------------------------------
# Enums and data classes
# ---------------------------------------------------------------------------

class AlertUrgency(str, Enum):
    WATCH    = "watch"     # trending toward threshold, > 30 min to breach
    WARNING  = "warning"   # 15-30 min to breach at current slope
    IMMINENT = "imminent"  # < 15 min to breach
    BREACHED = "breached"  # already over threshold (should have paged)


@dataclass
class TrendAnalysis:
    metric_name: str
    current_value: float
    threshold: float
    utilization_pct: float          # current / threshold * 100
    slope_per_minute: float         # rate of change (positive = growing)
    r_squared: float                # trend fit quality (0-1)
    estimated_minutes_to_breach: float | None  # None if not trending toward breach
    is_trending_toward_breach: bool


@dataclass
class PredictiveAlert:
    service: str
    metric_name: str
    incident_type: str              # predicted incident type if breach occurs
    urgency: AlertUrgency
    current_value: float
    threshold: float
    utilization_pct: float
    estimated_minutes_to_breach: float | None
    trend: TrendAnalysis
    recommended_action: str
    confidence: float               # 0.0-1.0
    reasoning: str


# ---------------------------------------------------------------------------
# Urgency weight constants (used in confidence calculation)
# ---------------------------------------------------------------------------

_URGENCY_WEIGHTS: dict[AlertUrgency, float] = {
    AlertUrgency.BREACHED: 1.0,
    AlertUrgency.IMMINENT: 1.0,
    AlertUrgency.WARNING:  0.8,
    AlertUrgency.WATCH:    0.6,
}

# ---------------------------------------------------------------------------
# Metric-name → incident type mapping
# Evaluated in order; first prefix/substring match wins.
# ---------------------------------------------------------------------------

_METRIC_TO_INCIDENT_TYPE: list[tuple[str, str]] = [
    ("memory_",           "oomkill"),
    ("cpu_",              "saturation"),
    ("error_rate_",       "error_spike"),
    ("latency_",          "latency"),
    ("response_time_",    "latency"),
    ("connection_pool_",  "timeout"),
    ("fd_count_",         "timeout"),
    ("disk_",             "saturation"),
    ("goroutine_count",   "silent_failure"),
]


def _metric_to_incident_type(metric_name: str) -> str:
    """Map a metric name to its predicted incident type."""
    lower = metric_name.lower()
    for prefix, incident_type in _METRIC_TO_INCIDENT_TYPE:
        if lower.startswith(prefix) or lower == prefix.rstrip("_"):
            return incident_type
    # Partial substring matches for metrics that don't start with the prefix
    # (e.g. "app_memory_heap_bytes")
    for prefix, incident_type in _METRIC_TO_INCIDENT_TYPE:
        if prefix.rstrip("_") in lower:
            return incident_type
    return "error_spike"


# ---------------------------------------------------------------------------
# Recommended actions
# ---------------------------------------------------------------------------

_ACTION_MAP: dict[str, dict[AlertUrgency, str]] = {
    "oomkill": {
        AlertUrgency.WATCH:    "Monitor memory usage; review recent deployments for memory leaks",
        AlertUrgency.WARNING:  "Increase memory limits or trigger a rolling restart before OOM",
        AlertUrgency.IMMINENT: "Scale horizontally now or restart high-memory pods immediately",
        AlertUrgency.BREACHED: "Urgent: flush old sessions and increase memory limits now",
    },
    "saturation": {
        AlertUrgency.WATCH:    "Monitor CPU utilization; check for thread contention",
        AlertUrgency.WARNING:  "Scale horizontally or throttle inbound traffic",
        AlertUrgency.IMMINENT: "Scale horizontally now to prevent service degradation",
        AlertUrgency.BREACHED: "Urgent: reduce load, add instances, check for goroutine leak",
    },
    "error_spike": {
        AlertUrgency.WATCH:    "Review recent deployments and error logs for early signals",
        AlertUrgency.WARNING:  "Investigate error patterns; prepare rollback plan",
        AlertUrgency.IMMINENT: "Rollback recent change or enable circuit breaker now",
        AlertUrgency.BREACHED: "Urgent: activate incident response, rollback latest deploy",
    },
    "latency": {
        AlertUrgency.WATCH:    "Check downstream dependency latency and connection pools",
        AlertUrgency.WARNING:  "Warm up caches, review slow queries, or scale read replicas",
        AlertUrgency.IMMINENT: "Flush slow queries, increase timeout budgets, scale now",
        AlertUrgency.BREACHED: "Urgent: shed non-critical traffic, scale read replicas now",
    },
    "timeout": {
        AlertUrgency.WATCH:    "Monitor connection pool exhaustion and file descriptor usage",
        AlertUrgency.WARNING:  "Flush old sessions and increase connection pool size",
        AlertUrgency.IMMINENT: "Flush old sessions and increase fd limits immediately",
        AlertUrgency.BREACHED: "Urgent: recycle connection pool; check for fd leak",
    },
    "silent_failure": {
        AlertUrgency.WATCH:    "Check goroutine count trends; inspect for blocked goroutines",
        AlertUrgency.WARNING:  "Profile goroutine stacks; look for goroutine leak patterns",
        AlertUrgency.IMMINENT: "Check for goroutine leak and restart affected workers now",
        AlertUrgency.BREACHED: "Urgent: restart service to recover from goroutine exhaustion",
    },
}

_DEFAULT_ACTION: dict[AlertUrgency, str] = {
    AlertUrgency.WATCH:    "Monitor metric closely; prepare runbook",
    AlertUrgency.WARNING:  "Investigate root cause; prepare remediation plan",
    AlertUrgency.IMMINENT: "Act now to prevent SLA breach",
    AlertUrgency.BREACHED: "Urgent: SLA already breached — activate incident response",
}


def _recommended_action(incident_type: str, urgency: AlertUrgency) -> str:
    """Return a human-readable recommended action for the given incident type and urgency."""
    return _ACTION_MAP.get(incident_type, _DEFAULT_ACTION).get(urgency, _DEFAULT_ACTION[urgency])


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze_trend(
    metric_name: str,
    time_series: list[tuple[float, float]],  # [(timestamp_epoch, value), ...]
    threshold: float,
    warning_pct: float = 0.70,  # warn when > 70% of threshold
) -> TrendAnalysis:
    """Compute linear trend slope and estimated time to threshold breach.

    Uses manual least-squares linear regression (no scipy dependency).
    R² is computed as 1 - SS_res/SS_tot.  Only flags a meaningful trend
    when R² > 0.5.

    Args:
        metric_name:  Name of the metric being analysed.
        time_series:  List of (epoch_seconds, value) tuples, chronological.
        threshold:    SLA threshold value.
        warning_pct:  Fraction of threshold at which to start tracking.

    Returns:
        TrendAnalysis dataclass.
    """
    # Guard: need at least 2 points for regression
    if len(time_series) < 2:
        current_value = time_series[0][1] if time_series else 0.0
        utilization_pct = (current_value / threshold * 100.0) if threshold else 0.0
        return TrendAnalysis(
            metric_name=metric_name,
            current_value=current_value,
            threshold=threshold,
            utilization_pct=round(utilization_pct, 2),
            slope_per_minute=0.0,
            r_squared=0.0,
            estimated_minutes_to_breach=None,
            is_trending_toward_breach=False,
        )

    # Convert epoch seconds → minutes relative to first point (numerically stable)
    t0 = time_series[0][0]
    xs = [(ts - t0) / 60.0 for ts, _ in time_series]
    ys = [v for _, v in time_series]
    n = len(xs)

    # Least-squares: y = slope * x + intercept
    sum_x   = sum(xs)
    sum_y   = sum(ys)
    sum_xx  = sum(x * x for x in xs)
    sum_xy  = sum(x * y for x, y in zip(xs, ys))

    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-12:
        # All timestamps identical — cannot compute slope
        slope = 0.0
        intercept = sum_y / n
    else:
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n

    # R² = 1 - SS_res / SS_tot
    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot < 1e-12:
        # All values identical — perfectly flat, R² undefined → treat as 0
        r_squared = 0.0
    else:
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r_squared = max(0.0, min(1.0, 1.0 - ss_res / ss_tot))

    # Current value is the last data point
    current_value = ys[-1]
    utilization_pct = (current_value / threshold * 100.0) if threshold else 0.0

    # Estimate time to breach: (threshold - current) / slope (in minutes)
    estimated_minutes_to_breach: float | None = None
    is_trending_toward_breach = False

    if slope > 0 and threshold > 0 and current_value < threshold:
        # Positive slope trending toward an upper threshold
        is_trending_toward_breach = True
        estimated_minutes_to_breach = (threshold - current_value) / slope

    return TrendAnalysis(
        metric_name=metric_name,
        current_value=current_value,
        threshold=threshold,
        utilization_pct=round(utilization_pct, 4),
        slope_per_minute=round(slope, 6),
        r_squared=round(r_squared, 4),
        estimated_minutes_to_breach=(
            round(estimated_minutes_to_breach, 2)
            if estimated_minutes_to_breach is not None else None
        ),
        is_trending_toward_breach=is_trending_toward_breach,
    )


def _classify_urgency(
    current_value: float,
    threshold: float,
    estimated_minutes_to_breach: float | None,
    is_trending_toward_breach: bool,
    r_squared: float,
) -> AlertUrgency | None:
    """Return the urgency level, or None if the trend does not warrant an alert."""
    # Already breached — regardless of R²
    if current_value > threshold:
        return AlertUrgency.BREACHED

    # Trend quality gate
    if r_squared <= 0.5:
        return None

    if not is_trending_toward_breach or estimated_minutes_to_breach is None:
        return None

    if estimated_minutes_to_breach < 15:
        return AlertUrgency.IMMINENT
    if estimated_minutes_to_breach < 30:
        return AlertUrgency.WARNING
    return AlertUrgency.WATCH


def detect_predictive_alerts(
    service: str,
    metrics: dict[str, Any],   # {metric_name: {time_series: [...], threshold: float}}
    min_utilization_to_alert: float = 0.60,
    min_slope_threshold: float = 0.01,  # ignore flat trends
) -> list[PredictiveAlert]:
    """Scan metrics for pre-incident signals.

    For each metric that is trending meaningfully toward its threshold,
    emit a PredictiveAlert.  Results are sorted by urgency (most urgent first),
    then by estimated minutes to breach ascending.

    Args:
        service:                  Service name (for alert labelling).
        metrics:                  Dict mapping metric_name → {time_series, threshold}.
        min_utilization_to_alert: Minimum current/threshold ratio to bother alerting.
        min_slope_threshold:      Minimum |slope| to consider a trend non-flat.

    Returns:
        List of PredictiveAlert objects, sorted most-urgent first.
    """
    alerts: list[PredictiveAlert] = []

    _urgency_order = {
        AlertUrgency.BREACHED: 0,
        AlertUrgency.IMMINENT: 1,
        AlertUrgency.WARNING:  2,
        AlertUrgency.WATCH:    3,
    }

    for metric_name, metric_data in metrics.items():
        time_series: list[tuple[float, float]] = metric_data.get("time_series", [])
        threshold: float = float(metric_data.get("threshold", 0.0))

        if not time_series or threshold <= 0:
            logger.debug("Skipping %s: empty time series or zero threshold", metric_name)
            continue

        trend = analyze_trend(metric_name, time_series, threshold)

        # Skip if utilization is too low (not worth alerting yet)
        utilization_ratio = trend.current_value / threshold if threshold else 0.0
        if trend.current_value <= threshold and utilization_ratio < min_utilization_to_alert:
            logger.debug(
                "Skipping %s: utilization %.1f%% < min %.1f%%",
                metric_name, utilization_ratio * 100, min_utilization_to_alert * 100,
            )
            continue

        # Skip flat trends (unless already breached)
        if trend.current_value <= threshold and abs(trend.slope_per_minute) < min_slope_threshold:
            logger.debug("Skipping %s: flat slope %.6f", metric_name, trend.slope_per_minute)
            continue

        urgency = _classify_urgency(
            current_value=trend.current_value,
            threshold=threshold,
            estimated_minutes_to_breach=trend.estimated_minutes_to_breach,
            is_trending_toward_breach=trend.is_trending_toward_breach,
            r_squared=trend.r_squared,
        )

        if urgency is None:
            logger.debug("Skipping %s: no actionable urgency (R²=%.3f)", metric_name, trend.r_squared)
            continue

        incident_type = _metric_to_incident_type(metric_name)
        recommended_action = _recommended_action(incident_type, urgency)

        urgency_weight = _URGENCY_WEIGHTS[urgency]
        # For BREACHED we use current/threshold ratio as a proxy for R² confidence
        if urgency == AlertUrgency.BREACHED:
            confidence = round(min(1.0, utilization_ratio) * urgency_weight, 4)
        else:
            confidence = round(trend.r_squared * urgency_weight, 4)

        # Build human-readable reasoning
        if urgency == AlertUrgency.BREACHED:
            reasoning = (
                f"{metric_name} is at {trend.current_value:.2f} which exceeds threshold "
                f"{threshold:.2f} ({trend.utilization_pct:.1f}% utilization). "
                "Immediate action required."
            )
        else:
            reasoning = (
                f"{metric_name} is at {trend.current_value:.2f} ({trend.utilization_pct:.1f}% of "
                f"threshold {threshold:.2f}) with slope {trend.slope_per_minute:.4f}/min "
                f"(R²={trend.r_squared:.3f}). "
                f"Estimated breach in {trend.estimated_minutes_to_breach:.1f} minutes."
            )

        alert = PredictiveAlert(
            service=service,
            metric_name=metric_name,
            incident_type=incident_type,
            urgency=urgency,
            current_value=trend.current_value,
            threshold=threshold,
            utilization_pct=trend.utilization_pct,
            estimated_minutes_to_breach=trend.estimated_minutes_to_breach,
            trend=trend,
            recommended_action=recommended_action,
            confidence=confidence,
            reasoning=reasoning,
        )
        alerts.append(alert)
        logger.info(
            "PredictiveAlert: service=%s metric=%s urgency=%s eta=%.1fmin confidence=%.3f",
            service, metric_name, urgency.value,
            trend.estimated_minutes_to_breach or 0.0,
            confidence,
        )

    # Sort: most urgent first, then by time-to-breach ascending (None last)
    def _sort_key(a: PredictiveAlert) -> tuple[int, float]:
        order = _urgency_order.get(a.urgency, 99)
        eta = a.estimated_minutes_to_breach if a.estimated_minutes_to_breach is not None else 1e9
        return (order, eta)

    alerts.sort(key=_sort_key)
    return alerts
