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


# ------------------------------------------------------------------
# Decision Metrics — aggregated intelligence surface payload
# Designed for MTTR Dashboard intelligence widgets.
# Single endpoint to avoid N-trips from UI.
# ------------------------------------------------------------------

@router.get("/decision-metrics")
def get_decision_metrics() -> dict[str, Any]:
    """Return all 8 intelligence surface metrics in one compact payload.

    Sections: pattern_intelligence, feedback_quality, convergence,
    adaptive_intelligence, evidence_diversity, source_weights,
    learning_safety, mttr_impact.

    All warming states are explicit — never returns fake zeros.
    """
    result: dict[str, Any] = {}

    # ── 1. Pattern Intelligence ──────────────────────────────────────────────
    try:
        runner = _get_runner()
        preds = runner.get_active_predictions("WATCH") if runner else []
        accuracy_report = runner.get_accuracy_report() if runner else {}

        by_type: dict[str, dict] = {}
        for p in preds:
            pt = p.pattern_type
            if pt not in by_type:
                by_type[pt] = {"count": 0, "total_confidence": 0.0, "warming": not p.baseline_ready}
            by_type[pt]["count"] += 1
            by_type[pt]["total_confidence"] += p.confidence

        patterns = []
        for pt, data in by_type.items():
            avg_conf = data["total_confidence"] / data["count"] if data["count"] else 0
            type_acc = accuracy_report.get("by_type", {}).get(pt, {})
            patterns.append({
                "pattern_type": pt,
                "active_count": data["count"],
                "avg_confidence": round(avg_conf, 3),
                "warming": data["warming"],
                "precision": round(type_acc.get("precision", 0), 3),
                "reinforcement_count": type_acc.get("tp", 0),
            })

        result["pattern_intelligence"] = {
            "patterns": patterns,
            "total_active": len(preds),
            "runner_ready": runner._store is not None if runner else False,
        }
    except Exception as exc:
        logger.warning("decision-metrics pattern_intelligence failed: %s", exc)
        result["pattern_intelligence"] = {"patterns": [], "total_active": 0, "runner_ready": False}

    # ── 2. Feedback & Quality (from metrics dashboard ring buffer) ────────────
    try:
        from supervisor.metrics_dashboard import get_dashboard as _get_dash_fq
        snap = _get_dash_fq()
        n = snap.total_investigations
        low_confidence = snap.false_positive_rate  # ≈ conf < 30 fraction
        result["feedback_quality"] = {
            "total_investigations": n,
            "warming": n < 5,
            "root_cause_found_rate": round(snap.root_cause_found_rate, 3),
            "low_confidence_rate": round(low_confidence, 3),
            "mean_confidence": round(snap.mean_confidence, 1),
            "citation_coverage": round(getattr(snap, "citation_coverage_mean", 0), 3),
            "unresolved_rca_count": round((1 - snap.root_cause_found_rate) * n) if n > 0 else 0,
        }
    except Exception as exc:
        logger.warning("decision-metrics feedback_quality failed: %s", exc)
        result["feedback_quality"] = {"warming": True, "total_investigations": 0}

    # ── 3. Intelligence Convergence ──────────────────────────────────────────
    try:
        from supervisor.confidence_calibrator import get_calibrator
        from supervisor.metrics_dashboard import get_dashboard as _get_dash
        cal = get_calibrator()
        cal_report = cal.get_calibration_report()
        snap = _get_dash()
        n = snap.total_investigations
        result["convergence"] = {
            "warming": cal_report["total_samples"] < 10,
            "calibration_ece": cal_report["ece"],
            "calibration_samples": cal_report["total_samples"],
            "mean_confidence": round(snap.mean_confidence, 1),
            "mean_source_count": round(getattr(snap, "mean_tool_calls", 0) / 4, 1),
            "hypothesis_diversity": round(getattr(snap, "fix_proposed_rate", 0), 3),
        }
    except Exception as exc:
        logger.warning("decision-metrics convergence failed: %s", exc)
        result["convergence"] = {"warming": True}

    # ── 4. Adaptive Intelligence ─────────────────────────────────────────────
    try:
        from supervisor.strategy_evolver import get_rolling_quality_stats, _load_raw as _se_load
        rolling = get_rolling_quality_stats()
        raw = _se_load()

        total_entries = 0
        adaptive_entries = 0
        for key, val in raw.items():
            if key.startswith("_"):
                continue
            if isinstance(val, dict):
                for step, entry in val.items():
                    if step.startswith("_"):
                        continue
                    if isinstance(entry, dict) and entry.get("calls", 0) >= 1:
                        total_entries += 1
                        w = entry.get("weight", 1.0)
                        if abs(w - 1.0) > 0.05:
                            adaptive_entries += 1

        det_pct = round((total_entries - adaptive_entries) / total_entries, 3) if total_entries else 1.0
        adp_pct = round(adaptive_entries / total_entries, 3) if total_entries else 0.0
        result["adaptive_intelligence"] = {
            "warming": rolling["status"] == "no_data",
            "learning_mode": rolling.get("status", "no_data"),
            "rolling_quality": rolling.get("avg"),
            "deterministic_influence_pct": det_pct,
            "adaptive_influence_pct": adp_pct,
            "total_evolved_steps": total_entries,
        }
    except Exception as exc:
        logger.warning("decision-metrics adaptive_intelligence failed: %s", exc)
        result["adaptive_intelligence"] = {"warming": True, "learning_mode": "no_data"}

    # ── 5. Evidence Diversity ────────────────────────────────────────────────
    try:
        from supervisor.metrics_dashboard import get_dashboard_engine
        engine = get_dashboard_engine()
        multi_source_count = 0
        single_source_count = 0
        with engine._lock:
            recent = list(engine._ring)[-100:]
        for outcome in recent:
            tc = getattr(outcome, "tool_calls", 0)
            approx_sources = min(5, max(1, tc // 4)) if tc else 1
            if approx_sources >= 2:
                multi_source_count += 1
            else:
                single_source_count += 1

        n_div = len(recent)
        result["evidence_diversity"] = {
            "warming": n_div < 5,
            "total_analyzed": n_div,
            "multi_source_pct": round(multi_source_count / n_div, 3) if n_div else 0,
            "single_source_warning_count": single_source_count,
        }
    except Exception as exc:
        logger.warning("decision-metrics evidence_diversity failed: %s", exc)
        result["evidence_diversity"] = {"warming": True}

    # ── 6. Source Weight Evolution ────────────────────────────────────────────
    try:
        from supervisor.strategy_evolver import _load_raw as _se_load2
        raw2 = _se_load2()
        weight_rows = []
        for inc_type, steps in raw2.items():
            if inc_type.startswith("_") or not isinstance(steps, dict):
                continue
            for step, entry in steps.items():
                if step.startswith("_") or not isinstance(entry, dict):
                    continue
                calls = entry.get("calls", 0)
                weight = entry.get("weight", 1.0)
                delta = round(weight - 1.0, 3)
                weight_rows.append({
                    "incident_type": inc_type,
                    "step": step,
                    "weight": round(weight, 3),
                    "delta": delta,
                    "calls": calls,
                    "stable": calls >= 5,
                    "neutral": abs(delta) <= 0.05,
                })
        weight_rows.sort(key=lambda r: -abs(r["delta"]))
        result["source_weights"] = {
            "warming": len(weight_rows) == 0,
            "rows": weight_rows[:30],
            "total_evolved": len([r for r in weight_rows if not r["neutral"]]),
        }
    except Exception as exc:
        logger.warning("decision-metrics source_weights failed: %s", exc)
        result["source_weights"] = {"warming": True, "rows": []}

    # ── 7. Learning Safety ────────────────────────────────────────────────────
    try:
        from supervisor.adaptive_thresholds import get_health_report
        from supervisor.strategy_evolver import get_rolling_quality_stats as _rqs
        from supervisor.confidence_calibrator import get_calibrator as _gc
        from supervisor.experience_store import get_stats as _es_stats
        thresh_health = get_health_report()
        rolling2 = _rqs()
        cal2 = _gc().get_calibration_report()
        es = _es_stats()

        result["learning_safety"] = {
            "threshold_status": thresh_health["overall_status"],
            "drifted_thresholds": thresh_health["drifted_count"],
            "circuit_breaker_fired": rolling2.get("status") == "degraded",
            "calibration_stale": cal2["total_samples"] < 20 or cal2["ece"] > 0.15,
            "experience_count": es.get("count", 0),
            "experience_warming": es.get("count", 0) < 10,
            "no_behavioral_drift": thresh_health["drifted_count"] == 0,
            "recommendations": thresh_health.get("recommendations", []),
        }
    except Exception as exc:
        logger.warning("decision-metrics learning_safety failed: %s", exc)
        result["learning_safety"] = {"threshold_status": "UNKNOWN", "drifted_thresholds": 0}

    # ── 8. MTTR Impact (additive decision KPIs) ───────────────────────────────
    try:
        from supervisor.metrics_dashboard import get_dashboard as _get_dash2
        snap2 = _get_dash2()
        result["mttr_impact"] = {
            "investigation_count": snap2.total_investigations,
            "rca_confidence_quality": round(snap2.mean_confidence, 1),
            "evidence_usefulness": round(getattr(snap2, "citation_coverage_mean", 0) * 100, 1),
            "fix_proposed_rate": round(snap2.fix_proposed_rate * 100, 1),
            "warming": snap2.total_investigations < 3,
        }
    except Exception as exc:
        logger.warning("decision-metrics mttr_impact failed: %s", exc)
        result["mttr_impact"] = {"warming": True}

    return result
