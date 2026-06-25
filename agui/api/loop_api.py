"""FastAPI router for loop engineering telemetry.

Endpoints:
  GET  /api/loop/metrics                  — list recent loop runs
  GET  /api/loop/metrics/{investigation_id} — single run detail
  POST /api/loop/reset                    — clear telemetry store
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/loop/metrics")
def list_loop_metrics(limit: int = 20) -> dict:
    try:
        from supervisor.loop_controller import list_telemetry
        items = list_telemetry(limit=limit)
        return {"total": len(items), "runs": items}
    except Exception as exc:
        return {"total": 0, "runs": [], "error": str(exc)}


@router.get("/api/loop/metrics/{investigation_id}")
def get_loop_metrics(investigation_id: str) -> dict:
    try:
        from supervisor.loop_controller import get_telemetry
        telemetry = get_telemetry(investigation_id)
        if telemetry is None:
            raise HTTPException(status_code=404, detail="No telemetry for this investigation")
        return telemetry.to_dict()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/loop/reset")
def reset_loop_metrics() -> dict:
    try:
        from supervisor.loop_controller import clear_telemetry
        n = clear_telemetry()
        return {"cleared": n}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
