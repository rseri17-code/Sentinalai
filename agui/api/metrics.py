"""AG UI Metrics API — investigation performance dashboard.

Routes:
  GET /api/v1/metrics/dashboard         → aggregate stats (MTTR, confidence, FP rate)
  GET /api/v1/metrics/trend             → time-series investigation counts
  GET /api/v1/metrics/calibration       → confidence calibration curve data
  GET /api/v1/metrics/intelligence      → pattern intelligence accuracy + SLO burn summary
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from agui.middleware.auth import ActorContext, get_actor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


@router.get("/dashboard")
async def get_metrics_dashboard(
    actor: ActorContext = Depends(get_actor),
):
    """Return aggregate investigation performance metrics.

    Includes:
    - MTTR (median, p95, p99)
    - Mean/median confidence, calibration error
    - Root-cause-found rate, false positive rate
    - Citation coverage mean
    - Fix proposed / applied / verified rates
    - Mean tool calls and LLM tokens per investigation
    - Breakdown by incident_type and severity
    - Last 24h / 7d investigation counts
    """
    from supervisor.metrics_dashboard import get_dashboard
    snapshot = get_dashboard()
    return snapshot.to_dict()


@router.get("/trend")
async def get_metrics_trend(
    window_hours: int = Query(default=24, ge=1, le=720, description="Lookback window in hours"),
    resolution_hours: int = Query(default=1, ge=1, le=24, description="Bucket size in hours"),
    actor: ActorContext = Depends(get_actor),
):
    """Return time-series investigation counts for trend analysis.

    Returns a list of time buckets with count, mean confidence,
    mean elapsed_ms, and root_cause_found count per bucket.
    """
    from supervisor.metrics_dashboard import get_dashboard_engine
    trend = get_dashboard_engine().get_trend(
        window_hours=window_hours,
        resolution_hours=resolution_hours,
    )
    return {
        "window_hours": window_hours,
        "resolution_hours": resolution_hours,
        "buckets": trend,
        "total_points": len(trend),
    }


@router.get("/calibration")
async def get_calibration_curve(
    buckets: int = Query(default=10, ge=5, le=20, description="Number of confidence buckets"),
    actor: ActorContext = Depends(get_actor),
):
    """Return confidence calibration curve data.

    Each bucket shows the mean predicted confidence vs actual correct rate.
    A perfectly calibrated model lies on the diagonal y=x.

    Use this chart to tune the ConfidenceCalibrator.
    """
    from supervisor.metrics_dashboard import get_dashboard_engine
    curve = get_dashboard_engine().get_calibration_curve(buckets=buckets)
    return {
        "buckets": buckets,
        "curve": curve,
        "note": "actual_correct_rate is proxied by (has_root_cause AND confidence >= 60)",
    }


@router.get("/mttr")
async def get_mttr_dashboard(
    window_hours: int = Query(default=168, ge=1, le=8760, description="Lookback window (default 7 days)"),
    human_baseline_minutes: float = Query(default=45.0, ge=1.0, le=480.0),
    actor: ActorContext = Depends(get_actor),
):
    """Full MTTR dashboard data in one call.

    Returns everything the MTTRDashboard component needs:
    - KPI summary (median/p95/p99 MTTR, root-cause rate, deflection rate)
    - 30-day daily trend (sparkline data)
    - Per-service breakdown table
    - ROI summary (time saved vs human baseline)
    - Calibration curve
    """
    from supervisor.metrics_dashboard import get_dashboard_engine
    engine = get_dashboard_engine()

    snapshot = engine.get_dashboard().to_dict()
    trend = engine.get_mttr_trend_by_day(window_days=30)
    service_breakdown = engine.get_service_breakdown(window_hours=window_hours)
    roi = engine.get_roi_summary(
        human_baseline_minutes=human_baseline_minutes,
        window_hours=window_hours,
    )
    calibration = engine.get_calibration_curve(buckets=10)

    return {
        "window_hours": window_hours,
        "kpis": {
            "mttr_median_ms": snapshot["mttr_median_ms"],
            "mttr_p95_ms": snapshot["mttr_p95_ms"],
            "mttr_p99_ms": snapshot["mttr_p99_ms"],
            "total_investigations": snapshot["total_investigations"],
            "last_24h_count": snapshot["last_24h_count"],
            "last_7d_count": snapshot["last_7d_count"],
            "root_cause_found_rate": snapshot["root_cause_found_rate"],
            "mean_confidence": snapshot["mean_confidence"],
            "fix_proposed_rate": snapshot["fix_proposed_rate"],
            "fix_applied_rate": snapshot["fix_applied_rate"],
        },
        "trend": trend,
        "service_breakdown": service_breakdown,
        "roi": roi,
        "calibration": calibration,
    }


async def get_intelligence_metrics(
    actor: ActorContext = Depends(get_actor),
):
    """Return Pattern Intelligence Layer accuracy and SLO health summary.

    Combines:
    - Prediction accuracy by pattern type (precision, TP/FP counts)
    - Active prediction counts by severity
    - SLO burn status summary (OK/BURNING/CRITICAL/BREACHED counts)
    - Total predictions tracked
    """
    from intelligence.background_runner import get_runner
    runner = get_runner()

    accuracy = runner.get_accuracy_report()

    active = runner.get_active_predictions("WATCH")
    severity_counts: dict[str, int] = {}
    for p in active:
        severity_counts[p.severity] = severity_counts.get(p.severity, 0) + 1

    slo_statuses = runner.get_slo_statuses()
    slo_summary: dict[str, int] = {}
    for s in slo_statuses:
        slo_summary[s.status] = slo_summary.get(s.status, 0) + 1

    return {
        "accuracy": accuracy,
        "active_predictions": {
            "total": len(active),
            "by_severity": severity_counts,
        },
        "slo_health": {
            "total_slos": len(slo_statuses),
            "by_status": slo_summary,
            "burning_or_worse": sum(
                slo_summary.get(s, 0) for s in ("BURNING", "CRITICAL", "BREACHED")
            ),
        },
        "runner_iteration": runner._iteration,
    }
