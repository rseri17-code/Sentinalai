"""Harness API — self-awareness and self-correction observability.

Routes:
  GET  /api/v1/harness/status              → learning stack health + harness config
  GET  /api/v1/harness/self-report         → full self-eval report (all learning components)
  GET  /api/v1/harness/reflection/{inv_id} → per-investigation reflection record
  POST /api/v1/harness/force-improve       → trigger nightly self-improvement now
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status as http_status

from agui.middleware.auth import ActorContext, get_actor, require_role
from agui.state_store import get_state_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/harness", tags=["harness"])


@router.get("/status")
async def get_harness_status(actor: ActorContext = Depends(get_actor)):
    """Return harness configuration and current learning stack health."""
    try:
        from supervisor.agent_harness import get_harness_status
        return get_harness_status()
    except Exception as exc:
        logger.warning("harness status failed: %s", exc)
        return {
            "harness_enabled": False,
            "error": str(exc),
            "overall_status": "UNKNOWN",
            "components": {},
        }


@router.get("/self-report")
async def get_self_report(actor: ActorContext = Depends(get_actor)):
    """Full self-evaluation report: calibration, strategy, thresholds, database."""
    try:
        from supervisor.learning_loop import generate_self_eval_report
        return generate_self_eval_report()
    except Exception as exc:
        logger.warning("self-report failed: %s", exc)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Self-report generation failed: {exc}",
        )


@router.get("/reflection/{investigation_id}")
async def get_reflection(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Return the harness reflection record for a completed investigation.

    The reflection is embedded in the investigation result under the
    `harness_reflection` key when HARNESS_ENABLED=true.
    """
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if state is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Investigation {investigation_id} not found",
        )

    # Reflection is persisted in state.result if present
    result = getattr(state, "result", None) or {}
    reflection = result.get("harness_reflection")
    if reflection is None:
        return {
            "investigation_id": investigation_id,
            "harness_enabled": False,
            "message": "No reflection data — investigation was run without harness mode.",
        }

    return reflection


@router.post("/force-improve")
async def force_self_improvement(
    actor: ActorContext = Depends(require_role("admin")),
):
    """Trigger the nightly self-improvement cycle immediately (admin only).

    Runs: threshold drift detection, calibrator rebuild if stale, full health report.
    """
    try:
        from supervisor.learning_loop import run_nightly_self_improvement
        import asyncio
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, run_nightly_self_improvement)
        return {
            "triggered": True,
            "summary": summary,
        }
    except Exception as exc:
        logger.warning("force-improve failed: %s", exc)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get("/calibration-history")
async def get_calibration_history(actor: ActorContext = Depends(get_actor)):
    """Return calibration bin data for plotting confidence reliability diagrams."""
    try:
        from supervisor.confidence_calibrator import get_calibrator, _calibrator_lock
        with _calibrator_lock:
            cal = get_calibrator()
            report = cal.get_calibration_report()

        bins = report.get("bins", [])
        return {
            "bins": bins,
            "ece": report.get("ece", 0),
            "total_samples": report.get("total_samples", 0),
            "last_updated": report.get("last_updated", ""),
            "stale": report.get("stale", False),
        }
    except Exception as exc:
        logger.warning("calibration history failed: %s", exc)
        return {"bins": [], "ece": 0, "total_samples": 0, "stale": True}
