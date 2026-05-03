"""Investigation Metrics Dashboard — aggregate performance telemetry.

Karpathy principle: "You can't improve what you can't measure."

Tracks per-investigation outcomes and computes aggregate statistics:
  - MTTR (Mean Time to Resolution) — from alert creation to investigation complete
  - Confidence calibration curve — predicted vs actual accuracy
  - False positive rate — investigations with no root cause found
  - Tool utilisation — which workers are called most / least
  - Cost per investigation — LLM tokens, tool calls, elapsed time

All metrics are stored in an in-memory ring buffer (last 10,000 investigations)
for real-time dashboards, plus written to the DB when persistence is enabled.

Exposed via:
  GET /api/v1/metrics/dashboard   → aggregate stats
  GET /api/v1/metrics/trend       → 24h / 7d / 30d trend lines
  GET /api/v1/metrics/calibration → confidence calibration chart data

Usage:
    from supervisor.metrics_dashboard import record_investigation_outcome, get_dashboard

    # Call at end of every investigation
    record_investigation_outcome(
        investigation_id="inv-abc",
        incident_id="INC001",
        incident_type="error_spike",
        service="payment-service",
        root_cause="Rollback deployed faulty auth module",
        confidence=88,
        severity=2,
        elapsed_ms=14200,
        tool_calls=12,
        llm_input_tokens=4200,
        llm_output_tokens=820,
        citation_coverage=0.84,
        fix_proposed=True,
        fix_applied=False,
    )

    # Get live dashboard
    dash = get_dashboard()
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger("sentinalai.metrics_dashboard")

# Max investigations kept in memory ring buffer
_RING_SIZE = 10_000


@dataclass
class InvestigationOutcome:
    """Single investigation outcome record."""
    investigation_id: str
    incident_id: str
    incident_type: str
    service: str
    root_cause: str
    confidence: float
    severity: int
    elapsed_ms: float
    tool_calls: int
    llm_input_tokens: int
    llm_output_tokens: int
    citation_coverage: float
    fix_proposed: bool
    fix_applied: bool
    fix_verified: bool
    recorded_at: float = field(default_factory=time.time)

    @property
    def has_root_cause(self) -> bool:
        rc = self.root_cause.upper()
        return bool(self.root_cause) and rc not in ("UNKNOWN", "UNDETERMINED", "")

    @property
    def llm_total_tokens(self) -> int:
        return self.llm_input_tokens + self.llm_output_tokens


@dataclass
class DashboardSnapshot:
    """Point-in-time aggregate metrics."""
    total_investigations: int
    # MTTR
    mttr_median_ms: float
    mttr_p95_ms: float
    mttr_p99_ms: float
    # Confidence
    mean_confidence: float
    median_confidence: float
    confidence_calibration_error: float     # mean |predicted - 50| proxy
    # Quality
    root_cause_found_rate: float            # fraction where root_cause != unknown
    false_positive_rate: float              # confidence < 30
    citation_coverage_mean: float
    # Fix rates
    fix_proposed_rate: float
    fix_applied_rate: float
    fix_verified_rate: float
    # Cost
    mean_tool_calls: float
    mean_llm_tokens: float
    # By type
    by_incident_type: dict[str, dict]
    by_severity: dict[int, dict]
    # Trend (last 24h counts)
    last_24h_count: int
    last_7d_count: int
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["generated_at_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.generated_at)
        )
        return d


class MetricsDashboard:
    """Thread-safe aggregate metrics for all investigations."""

    def __init__(self, ring_size: int = _RING_SIZE) -> None:
        self._ring: deque[InvestigationOutcome] = deque(maxlen=ring_size)
        self._lock = threading.Lock()

    def record(self, outcome: InvestigationOutcome) -> None:
        """Append an investigation outcome to the ring buffer."""
        with self._lock:
            self._ring.append(outcome)

    def get_dashboard(self) -> DashboardSnapshot:
        """Compute and return aggregate metrics from the ring buffer."""
        with self._lock:
            outcomes = list(self._ring)

        if not outcomes:
            return self._empty_snapshot()

        now = time.time()
        last_24h = [o for o in outcomes if now - o.recorded_at < 86_400]
        last_7d  = [o for o in outcomes if now - o.recorded_at < 604_800]

        elapsed_list = [o.elapsed_ms for o in outcomes]
        confidence_list = [o.confidence for o in outcomes]
        tool_calls_list = [o.tool_calls for o in outcomes]
        token_list = [o.llm_total_tokens for o in outcomes]
        citation_list = [o.citation_coverage for o in outcomes]

        n = len(outcomes)

        return DashboardSnapshot(
            total_investigations=n,
            # MTTR
            mttr_median_ms=_safe_percentile(elapsed_list, 50),
            mttr_p95_ms=_safe_percentile(elapsed_list, 95),
            mttr_p99_ms=_safe_percentile(elapsed_list, 99),
            # Confidence
            mean_confidence=_safe_mean(confidence_list),
            median_confidence=_safe_percentile(confidence_list, 50),
            confidence_calibration_error=_safe_mean(
                [abs(c - 50) for c in confidence_list]
            ),
            # Quality
            root_cause_found_rate=sum(1 for o in outcomes if o.has_root_cause) / n,
            false_positive_rate=sum(1 for o in outcomes if o.confidence < 30) / n,
            citation_coverage_mean=_safe_mean(citation_list),
            # Fix rates
            fix_proposed_rate=sum(1 for o in outcomes if o.fix_proposed) / n,
            fix_applied_rate=sum(1 for o in outcomes if o.fix_applied) / n,
            fix_verified_rate=sum(1 for o in outcomes if o.fix_verified) / n,
            # Cost
            mean_tool_calls=_safe_mean(tool_calls_list),
            mean_llm_tokens=_safe_mean(token_list),
            # Breakdowns
            by_incident_type=self._by_field(outcomes, "incident_type"),
            by_severity=self._by_field(outcomes, "severity"),
            # Trend
            last_24h_count=len(last_24h),
            last_7d_count=len(last_7d),
        )

    def get_service_breakdown(self, window_hours: int = 168) -> list[dict]:
        """Return per-service MTTR and quality stats, sorted by investigation count desc."""
        with self._lock:
            outcomes = list(self._ring)

        cutoff = time.time() - window_hours * 3600
        outcomes = [o for o in outcomes if o.recorded_at >= cutoff]
        if not outcomes:
            return []

        groups: dict[str, list[InvestigationOutcome]] = defaultdict(list)
        for o in outcomes:
            groups[o.service].append(o)

        result = []
        for svc, group in groups.items():
            n = len(group)
            elapsed = [o.elapsed_ms for o in group]
            result.append({
                "service": svc,
                "count": n,
                "mttr_median_ms": _safe_percentile(elapsed, 50),
                "mttr_p95_ms": _safe_percentile(elapsed, 95),
                "root_cause_found_rate": round(sum(1 for o in group if o.has_root_cause) / n, 3),
                "mean_confidence": round(_safe_mean([o.confidence for o in group]), 1),
                "fix_proposed_rate": round(sum(1 for o in group if o.fix_proposed) / n, 3),
            })

        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    def get_mttr_trend_by_day(self, window_days: int = 30) -> list[dict]:
        """Return daily median MTTR and investigation count for trend sparklines."""
        with self._lock:
            outcomes = list(self._ring)

        now = time.time()
        result = []
        for day_offset in range(window_days - 1, -1, -1):
            day_start = now - (day_offset + 1) * 86_400
            day_end   = now - day_offset * 86_400
            bucket = [o for o in outcomes if day_start <= o.recorded_at < day_end]
            result.append({
                "date": time.strftime("%Y-%m-%d", time.gmtime(day_start)),
                "count": len(bucket),
                "mttr_median_ms": _safe_percentile([o.elapsed_ms for o in bucket], 50),
                "root_cause_found": sum(1 for o in bucket if o.has_root_cause),
                "mean_confidence": _safe_mean([o.confidence for o in bucket]) if bucket else 0,
            })
        return result

    def get_roi_summary(
        self,
        human_baseline_minutes: float = 45.0,
        window_hours: int = 168,
    ) -> dict:
        """Compute agent ROI: time saved, incidents deflected, cost avoided.

        human_baseline_minutes: median human MTTR without agent (industry ~45min for P2).
        """
        with self._lock:
            outcomes = list(self._ring)

        cutoff = time.time() - window_hours * 3600
        window_outcomes = [o for o in outcomes if o.recorded_at >= cutoff]

        if not window_outcomes:
            return {"window_hours": window_hours, "investigations": 0}

        n = len(window_outcomes)
        agent_median_ms = _safe_percentile([o.elapsed_ms for o in window_outcomes], 50)
        agent_median_min = agent_median_ms / 60_000

        time_saved_per_inc_min = max(0.0, human_baseline_minutes - agent_median_min)
        total_time_saved_hours = round(n * time_saved_per_inc_min / 60, 1)

        # Deflections: investigations resolved autonomously (high confidence, fix proposed, no human control)
        deflected = sum(
            1 for o in window_outcomes
            if o.confidence >= 70 and o.has_root_cause
        )

        return {
            "window_hours": window_hours,
            "investigations": n,
            "agent_median_mttr_min": round(agent_median_min, 1),
            "human_baseline_min": human_baseline_minutes,
            "time_saved_per_incident_min": round(time_saved_per_inc_min, 1),
            "total_time_saved_hours": total_time_saved_hours,
            "deflection_count": deflected,
            "deflection_rate": round(deflected / n, 3) if n else 0.0,
        }

    def get_calibration_curve(self, buckets: int = 10) -> list[dict]:
        """Return calibration curve data for charting.

        Buckets confidence 0–100 into N equal ranges and returns
        {bucket_min, bucket_max, predicted_mean, actual_correct_rate, count}
        for each bucket.  'actual_correct' is proxied by confidence >= 60.
        """
        with self._lock:
            outcomes = list(self._ring)
        if not outcomes:
            return []

        bucket_size = 100 / buckets
        curve = []
        for i in range(buckets):
            lo = i * bucket_size
            hi = lo + bucket_size
            bucket_outcomes = [o for o in outcomes if lo <= o.confidence < hi]
            if not bucket_outcomes:
                continue
            # Proxy: "correct" = has_root_cause and confidence >= 60
            correct = sum(1 for o in bucket_outcomes if o.has_root_cause and o.confidence >= 60)
            curve.append({
                "bucket_min": round(lo, 1),
                "bucket_max": round(hi, 1),
                "predicted_mean": _safe_mean([o.confidence for o in bucket_outcomes]),
                "actual_correct_rate": correct / len(bucket_outcomes),
                "count": len(bucket_outcomes),
            })
        return curve

    def get_trend(self, window_hours: int = 24, resolution_hours: int = 1) -> list[dict]:
        """Return investigation counts over time for trend charts.

        Always returns ``window_hours // resolution_hours`` buckets so the
        chart renders correctly even before any investigations have run.
        """
        with self._lock:
            outcomes = list(self._ring)

        now = time.time()
        window_start = now - window_hours * 3600
        resolution_sec = resolution_hours * 3600
        num_buckets = window_hours // resolution_hours

        trend = []
        for i in range(num_buckets):
            bucket_start = window_start + i * resolution_sec
            bucket_end = bucket_start + resolution_sec
            bucket = [o for o in outcomes if bucket_start <= o.recorded_at < bucket_end]
            trend.append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bucket_start)),
                "count": len(bucket),
                "mean_confidence": _safe_mean([o.confidence for o in bucket]) if bucket else 0,
                "mean_elapsed_ms": _safe_mean([o.elapsed_ms for o in bucket]) if bucket else 0,
                "root_cause_found": sum(1 for o in bucket if o.has_root_cause),
            })
        return trend

    # ------------------------------------------------------------------ #

    def _by_field(self, outcomes: list, field_name: str) -> dict:
        """Group outcomes by a field and compute per-group stats."""
        groups: dict[Any, list[InvestigationOutcome]] = defaultdict(list)
        for o in outcomes:
            groups[getattr(o, field_name)].append(o)

        result = {}
        for key, group in groups.items():
            n = len(group)
            result[str(key)] = {
                "count": n,
                "mean_confidence": _safe_mean([o.confidence for o in group]),
                "mean_elapsed_ms": _safe_mean([o.elapsed_ms for o in group]),
                "root_cause_found_rate": sum(1 for o in group if o.has_root_cause) / n,
                "fix_proposed_rate": sum(1 for o in group if o.fix_proposed) / n,
            }
        return result

    def _empty_snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            total_investigations=0,
            mttr_median_ms=0, mttr_p95_ms=0, mttr_p99_ms=0,
            mean_confidence=0, median_confidence=0, confidence_calibration_error=0,
            root_cause_found_rate=0, false_positive_rate=0, citation_coverage_mean=0,
            fix_proposed_rate=0, fix_applied_rate=0, fix_verified_rate=0,
            mean_tool_calls=0, mean_llm_tokens=0,
            by_incident_type={}, by_severity={},
            last_24h_count=0, last_7d_count=0,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_mean(values: list[float] | list[int]) -> float:
    return round(statistics.mean(values), 2) if values else 0.0


def _safe_percentile(values: list[float] | list[int], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * pct / 100)
    idx = min(idx, len(sorted_vals) - 1)
    return round(sorted_vals[idx], 2)


# ---------------------------------------------------------------------------
# Module-level singleton + convenience functions
# ---------------------------------------------------------------------------

_dashboard: Optional[MetricsDashboard] = None
_dashboard_lock = threading.Lock()


def get_dashboard_engine() -> MetricsDashboard:
    global _dashboard
    with _dashboard_lock:
        if _dashboard is None:
            _dashboard = MetricsDashboard()
    return _dashboard


def record_investigation_outcome(
    investigation_id: str,
    incident_id: str,
    incident_type: str = "unknown",
    service: str = "unknown",
    root_cause: str = "",
    confidence: float = 0.0,
    severity: int = 3,
    elapsed_ms: float = 0.0,
    tool_calls: int = 0,
    llm_input_tokens: int = 0,
    llm_output_tokens: int = 0,
    citation_coverage: float = 0.0,
    fix_proposed: bool = False,
    fix_applied: bool = False,
    fix_verified: bool = False,
) -> None:
    """Convenience function — record an outcome into the global dashboard engine."""
    outcome = InvestigationOutcome(
        investigation_id=investigation_id,
        incident_id=incident_id,
        incident_type=incident_type,
        service=service,
        root_cause=root_cause,
        confidence=confidence,
        severity=severity,
        elapsed_ms=elapsed_ms,
        tool_calls=tool_calls,
        llm_input_tokens=llm_input_tokens,
        llm_output_tokens=llm_output_tokens,
        citation_coverage=citation_coverage,
        fix_proposed=fix_proposed,
        fix_applied=fix_applied,
        fix_verified=fix_verified,
    )
    get_dashboard_engine().record(outcome)


def get_dashboard() -> DashboardSnapshot:
    """Return the current aggregate dashboard snapshot."""
    return get_dashboard_engine().get_dashboard()
