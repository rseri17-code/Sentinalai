"""AG UI Metrics API — investigation performance dashboard.

Routes:
  GET /api/v1/metrics/dashboard    → aggregate stats (MTTR, confidence, FP rate)
  GET /api/v1/metrics/trend        → time-series investigation counts
  GET /api/v1/metrics/calibration  → confidence calibration curve data
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
