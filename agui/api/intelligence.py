"""Intelligence API — REST endpoints for the Pattern Intelligence Layer.

Endpoints:
  GET  /api/v1/intelligence/feed                 — SignalAlert feed (UI-native shape)
  POST /api/v1/intelligence/alerts/{id}/acknowledge — acknowledge a signal alert
  GET  /api/v1/intelligence/predictions          — raw predictions (min_severity filter)
  GET  /api/v1/intelligence/predictions/{id}     — single prediction detail
  POST /api/v1/intelligence/predictions/{id}/fp  — mark false positive
  GET  /api/v1/intelligence/slo                  — all SLO statuses
  GET  /api/v1/intelligence/slo/{service}        — SLO status for one service
  GET  /api/v1/intelligence/accuracy             — accuracy report by pattern type
  POST /api/v1/intelligence/outcomes             — record incident outcome (internal)
  GET  /api/v1/intelligence/health               — runner health / last cycle info
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("sentinalai.api.intelligence")

router = APIRouter(prefix="/api/v1/intelligence", tags=["intelligence"])


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class FalsePositiveRequest(BaseModel):
    reason: str = ""


class AcknowledgeRequest(BaseModel):
    actor: str = "operator"


class OutcomeRequest(BaseModel):
    service: str
    incident_id: str
    pattern_type: str = ""


# ------------------------------------------------------------------
# In-memory acknowledgement store (ephemeral — survives process restart)
# ------------------------------------------------------------------
_acks: dict[str, dict[str, str]] = {}   # prediction_id → {actor, acked_at}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_runner():
    from intelligence.background_runner import get_runner
    return get_runner()


_SEVERITY_TO_URGENCY = {"WATCH": "WATCH", "LIKELY": "WARNING", "IMMINENT": "IMMINENT"}
_METRIC_THRESHOLDS = {
    "error_rate": 0.01,         # 1% error rate
    "latency_p95_ms": 500.0,    # 500ms P95
    "saturation_pct": 80.0,     # 80% saturation
}
_PATTERN_TO_DIRECTION = {
    "trend_drift": "rising",
    "rate_accel": "rising",
    "slo_burn": "rising",
    "cross_service": "rising",
    "post_deploy": "rising",
}
_PATTERN_TO_ACTION = {
    "trend_drift":   "Investigate steady metric growth — check for resource leaks or traffic increase",
    "rate_accel":    "Metric doubling — check recent deployments and scale immediately if sustained",
    "cross_service": "Upstream dependency degrading — check {related_service} health first",
    "post_deploy":   "Post-deploy regression likely — consider rollback or immediate hotfix",
    "slo_burn":      "Error budget burning fast — reduce error rate to protect SLO target",
}


def _prediction_to_signal_alert(pred: Any) -> dict[str, Any]:
    ack = _acks.get(pred.prediction_id, {})
    threshold = _METRIC_THRESHOLDS.get(pred.metric, 1.0)
    action = _PATTERN_TO_ACTION.get(pred.pattern_type, pred.explanation)
    if pred.related_service:
        action = action.replace("{related_service}", pred.related_service)

    return {
        "id": pred.prediction_id,
        "service": pred.service,
        "metric_name": pred.metric,
        "current_value": round(pred.current_value, 6),
        "threshold": threshold,
        "urgency": _SEVERITY_TO_URGENCY.get(pred.severity, "WATCH"),
        "trend_direction": _PATTERN_TO_DIRECTION.get(pred.pattern_type, "rising"),
        "minutes_to_breach": (
            round(pred.predicted_breach_hours * 60, 0)
            if pred.predicted_breach_hours is not None else None
        ),
        "recommended_action": action,
        "confidence": round(pred.confidence, 3),
        "detected_at": pred.created_at,
        "acknowledged_by": ack.get("actor"),
        "acknowledged_at": ack.get("acked_at"),
        # Extra fields not in UI type but useful for detail views
        "pattern_type": pred.pattern_type,
        "explanation": pred.explanation,
        "evidence": pred.evidence,
        "related_service": pred.related_service,
    }


# ------------------------------------------------------------------
# Intelligence Feed (UI-native shape)
# ------------------------------------------------------------------

@router.get("/feed")
def get_feed(min_severity: str = Query("WATCH", pattern="^(WATCH|LIKELY|IMMINENT)$")) -> dict[str, Any]:
    runner = _get_runner()
    preds = runner.get_active_predictions(min_severity)
    alerts = [_prediction_to_signal_alert(p) for p in preds]
    return {"alerts": alerts, "total": len(alerts)}


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str, body: AcknowledgeRequest) -> dict[str, Any]:
    _acks[alert_id] = {
        "actor": body.actor,
        "acked_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"success": True, "alert_id": alert_id, "actor": body.actor}


# ------------------------------------------------------------------
# Predictions
# ------------------------------------------------------------------

@router.get("/predictions")
def list_predictions(
    min_severity: str = Query("WATCH", pattern="^(WATCH|LIKELY|IMMINENT)$"),
    service: str = Query("", description="Filter by service name"),
) -> dict[str, Any]:
    runner = _get_runner()
    preds = runner.get_active_predictions(min_severity)
    if service:
        preds = [p for p in preds if p.service == service]

    return {
        "predictions": [p.to_dict() for p in preds],
        "total": len(preds),
        "min_severity": min_severity,
    }


@router.get("/predictions/{prediction_id}")
def get_prediction(prediction_id: str) -> dict[str, Any]:
    runner = _get_runner()
    store = runner._store
    if store is None:
        raise HTTPException(503, "Intelligence runner not initialised")

    pred = store._predictions.get(prediction_id)
    if not pred:
        raise HTTPException(404, f"Prediction {prediction_id} not found")

    return pred.to_dict()


@router.post("/predictions/{prediction_id}/fp")
def mark_false_positive(
    prediction_id: str,
    body: FalsePositiveRequest,
) -> dict[str, Any]:
    runner = _get_runner()
    ok = runner.mark_false_positive(prediction_id, body.reason)
    if not ok:
        raise HTTPException(404, f"Prediction {prediction_id} not found")
    logger.info("Prediction %s marked as false positive via API: %s", prediction_id, body.reason)
    return {"success": True, "prediction_id": prediction_id}


# ------------------------------------------------------------------
# SLO
# ------------------------------------------------------------------

@router.get("/slo")
def list_slo_statuses() -> dict[str, Any]:
    runner = _get_runner()
    statuses = runner.get_slo_statuses()
    return {
        "slo_statuses": [s.to_dict() for s in statuses],
        "total": len(statuses),
        "burning": sum(1 for s in statuses if s.status in ("BURNING", "CRITICAL", "BREACHED")),
    }


@router.get("/slo/{service}")
def get_slo_for_service(service: str) -> dict[str, Any]:
    runner = _get_runner()
    statuses = runner.get_slo_statuses()
    svc_statuses = [s for s in statuses if s.service == service]
    if not svc_statuses:
        raise HTTPException(404, f"No SLO found for service '{service}'")
    return {
        "service": service,
        "slo_statuses": [s.to_dict() for s in svc_statuses],
    }


# ------------------------------------------------------------------
# Accuracy
# ------------------------------------------------------------------

@router.get("/accuracy")
def get_accuracy_report() -> dict[str, Any]:
    runner = _get_runner()
    return runner.get_accuracy_report()


# ------------------------------------------------------------------
# Outcome recording (called by investigation agent on incident close)
# ------------------------------------------------------------------

@router.post("/outcomes")
def record_outcome(body: OutcomeRequest) -> dict[str, Any]:
    runner = _get_runner()
    resolved = runner.record_outcome(body.service, body.incident_id, body.pattern_type)
    logger.info(
        "Outcome recorded via API: service=%s incident=%s resolved=%d",
        body.service, body.incident_id, resolved,
    )
    return {"success": True, "predictions_resolved": resolved}


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@router.get("/health")
def intelligence_health() -> dict[str, Any]:
    runner = _get_runner()
    return {
        "enabled": runner._task is not None and not (runner._task.done() if runner._task else True),
        "iteration": runner._iteration,
        "components_ready": runner._store is not None,
    }
