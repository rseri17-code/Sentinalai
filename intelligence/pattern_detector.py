"""Pattern Detector — statistical anomaly and pattern detection.

Five concrete, explainable algorithms. No black boxes.
Every detection produces a human-readable explanation.

Algorithms:
  1. Trend drift      — linear regression slope over last 30 points
  2. Rate of change   — acceleration: second derivative of metric
  3. Cross-service    — Pearson correlation between service metric series
  4. Post-deploy      — metric mean before vs. after last N deploys
  5. SLO burn rate    — from SLOEngine (already computed)

Each algorithm returns a Detection with:
  - service, pattern_type, severity (WATCH | LIKELY | IMMINENT)
  - confidence (0.0–1.0)
  - explanation (plain English — shown directly in Intelligence Feed)
  - predicted_breach_hours (best estimate)
  - evidence (raw numbers used in the calculation)

Design principle: if you can't explain the detection in one sentence,
the algorithm is wrong.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sentinalai.pattern_detector")

PATTERN_DETECTOR_ENABLED = os.environ.get(
    "PATTERN_DETECTOR_ENABLED", "true"
).lower() in ("1", "true", "yes")

# Minimum data points before making a detection
MIN_POINTS_TREND = 10
MIN_POINTS_CORRELATION = 20

# Thresholds
SLOPE_WARN_THRESHOLD   = 0.001   # error_rate increase per second → alert
SLOPE_CRIT_THRESHOLD   = 0.005
ROC_ACCEL_WARN         = 0.50    # 50% increase in last interval vs. previous
ROC_ACCEL_CRIT         = 1.00    # 100% increase (doubling)
CORRELATION_MIN_R      = 0.75    # minimum Pearson r to call a cross-service pattern
DEPLOY_DELTA_WARN      = 0.30    # 30% metric increase post-deploy
DEPLOY_DELTA_CRIT      = 0.75    # 75% increase


@dataclass
class Detection:
    """A detected pattern or anomaly."""
    service: str
    pattern_type: str        # trend_drift | rate_accel | cross_service | post_deploy | slo_burn
    severity: str            # WATCH | LIKELY | IMMINENT
    confidence: float        # 0.0–1.0
    metric: str
    current_value: float
    explanation: str         # one plain-English sentence
    predicted_breach_hours: float | None = None
    evidence: dict = field(default_factory=dict)
    related_service: str = ""    # for cross-service patterns

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "pattern_type": self.pattern_type,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "metric": self.metric,
            "current_value": round(self.current_value, 6),
            "explanation": self.explanation,
            "predicted_breach_hours": (
                round(self.predicted_breach_hours, 1)
                if self.predicted_breach_hours is not None else None
            ),
            "evidence": self.evidence,
            "related_service": self.related_service,
        }


class PatternDetector:
    """Runs all five detection algorithms against telemetry data."""

    def detect_all(
        self,
        services: list[str],
        aggregator: Any,
        slo_statuses: list[Any] | None = None,
    ) -> list[Detection]:
        """Run all detectors across all services.

        Args:
            services:     list of service names to analyse
            aggregator:   TelemetryAggregator for metric retrieval
            slo_statuses: pre-computed SLOStatus list (avoids re-querying)

        Returns all detections, highest severity first.
        """
        if not PATTERN_DETECTOR_ENABLED:
            return []

        detections: list[Detection] = []

        for service in services:
            if not aggregator.is_baseline_ready(service):
                logger.debug("Skipping %s — baseline not ready", service)
                continue

            for metric in ("error_rate", "latency_p95_ms", "saturation_pct"):
                series = aggregator.get_recent(service, minutes=60, metric=metric)
                if len(series) < MIN_POINTS_TREND:
                    continue

                d = self._detect_trend(service, metric, series)
                if d:
                    detections.append(d)

                d = self._detect_rate_accel(service, metric, series)
                if d:
                    detections.append(d)

            detections.extend(self._detect_post_deploy(service, aggregator))

        # Cross-service correlation (requires pairs)
        detections.extend(self._detect_cross_service(services, aggregator))

        # SLO burn from pre-computed statuses
        if slo_statuses:
            for status in slo_statuses:
                d = self._detection_from_slo(status)
                if d:
                    detections.append(d)

        # Sort: IMMINENT first, then LIKELY, then WATCH; highest confidence first
        _order = {"IMMINENT": 0, "LIKELY": 1, "WATCH": 2}
        detections.sort(key=lambda x: (_order.get(x.severity, 3), -x.confidence))

        logger.info("Pattern detector: %d detections across %d services", len(detections), len(services))
        return detections

    # ------------------------------------------------------------------
    # Algorithm 1: Trend drift (linear regression slope)
    # ------------------------------------------------------------------

    def _detect_trend(
        self, service: str, metric: str, series: list[tuple[float, float]]
    ) -> Detection | None:
        """Detect a sustained upward trend via linear regression slope."""
        if len(series) < MIN_POINTS_TREND:
            return None

        recent = series[-30:]   # last 30 points
        slope, r_squared = _linear_regression_slope(recent)

        if slope <= 0:
            return None   # flat or improving

        if slope >= SLOPE_CRIT_THRESHOLD and r_squared >= 0.6:
            severity = "IMMINENT"
            confidence = min(1.0, r_squared * (slope / SLOPE_CRIT_THRESHOLD))
        elif slope >= SLOPE_WARN_THRESHOLD and r_squared >= 0.5:
            severity = "LIKELY"
            confidence = min(0.85, r_squared * (slope / SLOPE_WARN_THRESHOLD) * 0.7)
        else:
            return None

        current = recent[-1][1]
        # Estimate time to breach 1% error rate (or latency threshold)
        target = 0.01 if "error" in metric else 500.0
        if slope > 0 and current < target:
            breach_secs = (target - current) / slope
            breach_hours = breach_secs / 3600
        else:
            breach_hours = None

        return Detection(
            service=service,
            pattern_type="trend_drift",
            severity=severity,
            confidence=confidence,
            metric=metric,
            current_value=current,
            explanation=(
                f"{service} {metric} rising steadily at "
                f"{slope*3600:.4f}/hour (R²={r_squared:.2f}) — "
                f"{'breach likely within {:.0f}h'.format(breach_hours) if breach_hours else 'trend continues'}"
            ),
            predicted_breach_hours=breach_hours,
            evidence={"slope_per_sec": round(slope, 8), "r_squared": round(r_squared, 3),
                      "window_points": len(recent), "current": round(current, 6)},
        )

    # ------------------------------------------------------------------
    # Algorithm 2: Rate-of-change acceleration
    # ------------------------------------------------------------------

    def _detect_rate_accel(
        self, service: str, metric: str, series: list[tuple[float, float]]
    ) -> Detection | None:
        """Detect sudden acceleration — metric doubling every few polls."""
        if len(series) < 4:
            return None

        # Compare last 3 points against prior 3 points
        recent_vals = [v for _, v in series[-3:]]
        prior_vals  = [v for _, v in series[-6:-3]]
        if not prior_vals:
            return None

        recent_avg = sum(recent_vals) / len(recent_vals)
        prior_avg  = sum(prior_vals) / len(prior_vals)

        if prior_avg <= 0:
            return None

        pct_change = (recent_avg - prior_avg) / prior_avg

        if pct_change >= ROC_ACCEL_CRIT:
            severity = "IMMINENT"
            confidence = min(1.0, 0.6 + pct_change * 0.1)
        elif pct_change >= ROC_ACCEL_WARN:
            severity = "LIKELY"
            confidence = min(0.8, 0.5 + pct_change * 0.15)
        else:
            return None

        # Rough doubling time estimate
        if pct_change > 0:
            intervals = 3  # averaging 3 polling intervals
            doubling_intervals = math.log(2) / math.log(1 + pct_change / intervals)
            doubling_hours = doubling_intervals * (60 / 3600)   # assuming 60s intervals
        else:
            doubling_hours = None

        return Detection(
            service=service,
            pattern_type="rate_accel",
            severity=severity,
            confidence=confidence,
            metric=metric,
            current_value=recent_avg,
            explanation=(
                f"{service} {metric} accelerating: "
                f"{prior_avg:.4f} → {recent_avg:.4f} "
                f"({pct_change*100:+.0f}% in last 3 polls)"
            ),
            predicted_breach_hours=doubling_hours,
            evidence={"prior_avg": round(prior_avg, 6), "recent_avg": round(recent_avg, 6),
                      "pct_change": round(pct_change, 4)},
        )

    # ------------------------------------------------------------------
    # Algorithm 3: Cross-service correlation
    # ------------------------------------------------------------------

    def _detect_cross_service(
        self, services: list[str], aggregator: Any
    ) -> list[Detection]:
        """Detect leading-indicator correlations between service error rates.

        For every pair (A, B): if A's metric 10 minutes ago correlates strongly
        with B's metric now, A is a leading indicator of B's failures.
        """
        detections: list[Detection] = []
        if len(services) < 2:
            return detections

        # Cache series to avoid repeated DB queries
        series_cache: dict[str, list[tuple[float, float]]] = {}
        for svc in services:
            series_cache[svc] = aggregator.get_recent(svc, minutes=60, metric="error_rate")

        for i, svc_a in enumerate(services):
            for svc_b in services[i + 1:]:
                series_a = series_cache[svc_a]
                series_b = series_cache[svc_b]
                if len(series_a) < MIN_POINTS_CORRELATION or len(series_b) < MIN_POINTS_CORRELATION:
                    continue

                r = _pearson_correlation(
                    [v for _, v in series_a],
                    [v for _, v in series_b],
                )
                if r >= CORRELATION_MIN_R:
                    # Determine which is the leader (higher current value → degrading first)
                    curr_a = series_a[-1][1]
                    curr_b = series_b[-1][1]
                    leader = svc_a if curr_a >= curr_b else svc_b
                    follower = svc_b if leader == svc_a else svc_a
                    leader_val = max(curr_a, curr_b)

                    detections.append(Detection(
                        service=follower,
                        pattern_type="cross_service",
                        severity="LIKELY" if r < 0.90 else "IMMINENT",
                        confidence=round(r, 3),
                        metric="error_rate",
                        current_value=min(curr_a, curr_b),
                        explanation=(
                            f"{leader} error_rate is rising (r={r:.2f} correlation with {follower}) — "
                            f"{follower} historically degrades when {leader} does"
                        ),
                        evidence={"pearson_r": round(r, 4),
                                  f"{leader}_error_rate": round(leader_val, 6)},
                        related_service=leader,
                    ))
        return detections

    # ------------------------------------------------------------------
    # Algorithm 4: Post-deploy degradation
    # ------------------------------------------------------------------

    def _detect_post_deploy(self, service: str, aggregator: Any) -> list[Detection]:
        """Compare error rate before vs. after the most recent deploy."""
        detections: list[Detection] = []
        # Get recent snapshot to check deploy flag
        recent = aggregator.get_recent(service, minutes=35, metric="error_rate")
        if not recent:
            return detections

        # Find deployment boundary in the series
        # We use a 30-minute window: pre = before last 30 min, post = last 30 min
        if len(recent) < 6:
            return detections

        mid = len(recent) // 2
        pre_vals  = [v for _, v in recent[:mid]]
        post_vals = [v for _, v in recent[mid:]]

        pre_avg  = sum(pre_vals) / len(pre_vals) if pre_vals else 0
        post_avg = sum(post_vals) / len(post_vals) if post_vals else 0

        if pre_avg <= 0 or post_avg <= pre_avg:
            return detections

        delta = (post_avg - pre_avg) / pre_avg

        if delta >= DEPLOY_DELTA_CRIT:
            severity, confidence = "IMMINENT", min(0.95, 0.7 + delta * 0.1)
        elif delta >= DEPLOY_DELTA_WARN:
            severity, confidence = "LIKELY", min(0.80, 0.55 + delta * 0.1)
        else:
            return detections

        detections.append(Detection(
            service=service,
            pattern_type="post_deploy",
            severity=severity,
            confidence=confidence,
            metric="error_rate",
            current_value=post_avg,
            explanation=(
                f"{service} error_rate increased {delta*100:.0f}% "
                f"({pre_avg:.4f} → {post_avg:.4f}) — possible deploy regression"
            ),
            evidence={"pre_avg": round(pre_avg, 6), "post_avg": round(post_avg, 6),
                      "delta_pct": round(delta * 100, 1)},
        ))
        return detections

    # ------------------------------------------------------------------
    # Algorithm 5: SLO burn → Detection
    # ------------------------------------------------------------------

    def _detection_from_slo(self, status: Any) -> Detection | None:
        """Convert a SLOStatus into a Detection if burn rate is dangerous."""
        if status.status == "OK" or status.status == "WATCHING":
            return None

        severity_map = {"BURNING": "LIKELY", "CRITICAL": "IMMINENT", "BREACHED": "IMMINENT"}
        severity = severity_map.get(status.status, "WATCH")
        confidence = min(1.0, status.burn_rate / 10.0)

        breach_h = None if status.hours_to_breach == float("inf") else status.hours_to_breach

        return Detection(
            service=status.service,
            pattern_type="slo_burn",
            severity=severity,
            confidence=confidence,
            metric=status.metric,
            current_value=status.current_value,
            explanation=(
                f"{status.service} SLO burning at {status.burn_rate:.1f}× rate — "
                f"{status.budget_remaining_pct:.0f}% error budget remaining"
                + (f", breach in ~{breach_h:.0f}h" if breach_h else "")
            ),
            predicted_breach_hours=breach_h,
            evidence={
                "burn_rate": round(status.burn_rate, 3),
                "budget_remaining_pct": round(status.budget_remaining_pct, 1),
                "slo_target": status.slo_target,
            },
        )


# ------------------------------------------------------------------
# Statistical helpers
# ------------------------------------------------------------------

def _linear_regression_slope(
    series: list[tuple[float, float]],
) -> tuple[float, float]:
    """Return (slope, r_squared) for a time series.

    slope units: value_units per second.
    """
    n = len(series)
    if n < 2:
        return 0.0, 0.0

    xs = [t for t, _ in series]
    ys = [v for _, v in series]

    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    ss_yy = sum((y - y_mean) ** 2 for y in ys)

    if ss_xx == 0:
        return 0.0, 0.0

    slope = ss_xy / ss_xx
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx * ss_yy > 0 else 0.0
    return slope, min(1.0, max(0.0, r_squared))


def _pearson_correlation(xs: list[float], ys: list[float]) -> float:
    """Compute Pearson r between two series (same length, aligned)."""
    n = min(len(xs), len(ys))
    if n < 4:
        return 0.0

    xs, ys = xs[-n:], ys[-n:]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - y_mean) ** 2 for y in ys))

    if den_x == 0 or den_y == 0:
        return 0.0
    return max(-1.0, min(1.0, num / (den_x * den_y)))
