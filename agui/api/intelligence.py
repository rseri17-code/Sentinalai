"""Intelligence API — proactive sentinel loop feed and prediction endpoints.

GET  /api/v1/intelligence/feed           — Active pre-incident signals from sentinel loop
GET  /api/v1/intelligence/predict        — On-demand prediction for a single service
POST /api/v1/intelligence/alerts/{id}/acknowledge  — Acknowledge a signal alert
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("sentinalai.agui.intelligence")

router = APIRouter(prefix="/api/v1/intelligence", tags=["intelligence"])

# In-memory alert store (replace with Redis/DynamoDB in production)
_ALERT_STORE: dict[str, dict] = {}


@router.get("/feed")
async def get_intelligence_feed() -> JSONResponse:
    """Return all active pre-incident signals from the sentinel loop."""
    alerts = sorted(
        _ALERT_STORE.values(),
        key=lambda a: {"BREACHED": 0, "IMMINENT": 1, "WARNING": 2, "WATCH": 3}.get(
            a.get("urgency", "WATCH"), 9
        ),
    )
    return JSONResponse({"alerts": alerts, "count": len(alerts)})


@router.get("/predict")
async def predict_service(
    service: str = Query(..., description="Service name"),
    hours: int = Query(4, description="Forecast horizon in hours"),
) -> JSONResponse:
    """Run predictive signal detection for a single service on demand."""
    from supervisor.sentinel_loop import run_prediction_for_service

    # Try to get live metrics from metrics worker if available
    metrics_snapshot: dict[str, Any] = {}
    try:
        from workers.metrics_worker import MetricsWorker
        worker = MetricsWorker()
        result = worker.execute(
            "query_metrics",
            {
                "service": service,
                "metrics": [
                    "cpu_utilisation",
                    "memory_utilisation",
                    "error_rate",
                    "p95_latency_ms",
                    "connection_pool_utilisation",
                ],
                "window": "30m",
            },
        )
        metrics_snapshot = result.get("metrics", {})
    except Exception:
        pass  # Sentinel loop handles empty metrics gracefully

    alerts = run_prediction_for_service(
        service=service,
        metrics_snapshot=metrics_snapshot,
        post_to_slack=False,
    )

    # Persist to alert store
    for alert in alerts:
        alert_id = str(uuid.uuid4())
        alert["id"] = alert_id
        alert["detected_at"] = datetime.now(timezone.utc).isoformat()
        _ALERT_STORE[alert_id] = alert

    return JSONResponse({"alerts": alerts, "service": service, "horizon_hours": hours})


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str, body: dict = {}) -> JSONResponse:
    """Acknowledge a pre-incident alert (suppresses repeat notifications)."""
    if alert_id not in _ALERT_STORE:
        return JSONResponse({"ok": False, "error": "alert_not_found"}, status_code=404)

    actor = body.get("actor", "unknown")
    _ALERT_STORE[alert_id]["acknowledged_by"] = actor
    _ALERT_STORE[alert_id]["acknowledged_at"] = datetime.now(timezone.utc).isoformat()

    logger.info("Alert %s acknowledged by %s", alert_id, actor)
    return JSONResponse({"ok": True, "alert_id": alert_id, "acknowledged_by": actor})


def ingest_sentinel_alert(alert: dict) -> str:
    """Called by the sentinel loop to register a new alert. Returns the alert ID."""
    alert_id = alert.get("id") or str(uuid.uuid4())
    alert["id"] = alert_id
    if "detected_at" not in alert:
        alert["detected_at"] = datetime.now(timezone.utc).isoformat()
    _ALERT_STORE[alert_id] = alert
    return alert_id
