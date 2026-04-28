"""AG UI Replay API.

Routes:
  POST /api/v1/investigations/{id}/replay          → Start replay session
  GET  /api/v1/investigations/{id}/replay/{sid}    → Get replay session status
  POST /api/v1/investigations/{id}/replay/{sid}/control → Control replay (pause/resume/step/abort)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from agui.middleware.auth import ActorContext, require_role, get_actor
from agui.state_store import get_state_store
from agui.receipt_store import get_receipt_store
from agui.replay_engine import get_replay_engine, ReplayMode
from agui.event_bus import get_bus
from agui.schemas.events import AGUIEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/investigations", tags=["replay"])


class StartReplayRequest(BaseModel):
    mode: str = "fast"         # live | fast | step
    speed_multiplier: float = 5.0


class ReplayControlRequest(BaseModel):
    action: str                # pause | resume | step | abort


@router.post("/{investigation_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def start_replay(
    investigation_id: str,
    req: StartReplayRequest,
    actor: ActorContext = Depends(require_role("operator")),
):
    """
    Start a deterministic replay of a completed investigation.

    Replay re-streams the original event sequence to all WebSocket subscribers.
    Does NOT re-execute any agent code.

    Subscribe to ws://.../ws/investigations/{investigation_id} to receive replay events.
    """
    # Load snapshot
    receipt_store = get_receipt_store()
    snapshot_data = await receipt_store.get_replay_snapshot(investigation_id)
    if not snapshot_data:
        # Try to build snapshot from stored events
        store = get_state_store()
        state = await store.get_state(investigation_id)
        if not state:
            raise HTTPException(status_code=404, detail="Investigation not found")

        events = await store.get_events(investigation_id)
        if not events:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="No events available for replay. Investigation may not be complete.",
            )

        engine = get_replay_engine()
        snapshot = engine.build_snapshot(
            investigation_id=investigation_id,
            incident_id=state.incident_id,
            trace_id=state.trace_id,
            events=events,
            original_duration_ms=state.duration_ms or 0.0,
        )
    else:
        from agui.replay_engine import ReplaySnapshot
        snapshot = ReplaySnapshot.from_dict(snapshot_data)

    # Validate before starting
    engine = get_replay_engine()
    validation = engine.validate(snapshot)
    if not validation.is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Replay validation failed",
                "errors": validation.errors,
                "gaps": validation.gaps,
                "hash_mismatches": validation.hash_mismatches,
            },
        )

    # Start replay — events are streamed via event bus to WebSocket subscribers
    bus = get_bus()

    async def replay_callback(event: AGUIEvent, step: int, total: int) -> None:
        # Tag event as replay
        event.payload["_replay"] = True
        event.payload["_replay_step"] = step
        event.payload["_replay_total"] = total
        await bus.publish(event)

    try:
        mode = ReplayMode(req.mode)
    except ValueError:
        mode = ReplayMode.FAST

    session = await engine.start_replay(
        snapshot=snapshot,
        actor_id=actor.actor_id,
        mode=mode,
        speed_multiplier=req.speed_multiplier,
        callback=replay_callback,
    )

    return {
        "session_id": session.session_id,
        "investigation_id": investigation_id,
        "mode": mode.value,
        "total_steps": session.total_steps,
        "status": session.status.value,
        "validation": {
            "is_valid": validation.is_valid,
            "event_count": validation.event_count,
            "gaps": validation.gaps,
        },
    }


@router.get("/{investigation_id}/replay/{session_id}")
async def get_replay_session(
    investigation_id: str,
    session_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Get current status of a replay session."""
    engine = get_replay_engine()
    session = engine.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Replay session not found")
    if session.investigation_id != investigation_id:
        raise HTTPException(status_code=403, detail="Session does not belong to this investigation")

    return {
        "session_id": session_id,
        "investigation_id": investigation_id,
        "status": session.status.value,
        "mode": session.mode.value,
        "current_step": session.current_step,
        "total_steps": session.total_steps,
        "progress_pct": (session.current_step / session.total_steps * 100)
            if session.total_steps > 0 else 0,
        "actor_id": session.actor_id,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
    }


@router.post("/{investigation_id}/replay/{session_id}/control")
async def control_replay(
    investigation_id: str,
    session_id: str,
    req: ReplayControlRequest,
    actor: ActorContext = Depends(require_role("operator")),
):
    """Control a replay session (pause/resume/step/abort)."""
    engine = get_replay_engine()
    session = engine.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Replay session not found")
    if session.investigation_id != investigation_id:
        raise HTTPException(status_code=403, detail="Session mismatch")

    action = req.action.lower()
    success = False

    if action == "pause":
        success = engine.pause(session_id)
    elif action == "resume":
        success = engine.resume(session_id)
    elif action == "step":
        success = engine.step(session_id)
    elif action == "abort":
        success = engine.abort(session_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    return {
        "session_id": session_id,
        "action": action,
        "success": success,
        "status": session.status.value if session else "unknown",
        "current_step": session.current_step if session else 0,
    }
