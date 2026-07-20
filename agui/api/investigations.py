"""AG UI Investigations API.

Routes:
  POST /api/v1/investigations          → Start new investigation
  GET  /api/v1/investigations          → List investigations (paginated)
  GET  /api/v1/investigations/{id}     → Get investigation state
  GET  /api/v1/investigations/{id}/graph → Get execution DAG
  GET  /api/v1/investigations/{id}/risk  → Get risk + confidence
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from agui.middleware.auth import ActorContext, require_role, get_actor
from agui.state_store import get_state_store
from agui.graph_builder import get_builder, rebuild_from_events
from agui.schemas.incidents import IncidentState, InvestigationStatus
from agui.schemas.graph import ExecutionGraph

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


class StartInvestigationRequest(BaseModel):
    incident_id: str
    priority: str = "normal"   # normal | high | critical
    context: dict = {}


class RiskResponse(BaseModel):
    investigation_id: str
    confidence: float
    risk_level: str
    stale_sources: list[str]
    budget_used: int
    budget_max: int
    budget_pct: float
    judge_scores: dict
    data_freshness: dict
    is_stale: bool


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def start_investigation(
    req: StartInvestigationRequest,
    request: Request,
    actor: ActorContext = Depends(require_role("operator")),
):
    """
    Trigger a new investigation for an incident.

    This endpoint:
    1. Creates an IncidentState record in the state store
    2. Dispatches the investigation to the agent (async)
    3. Returns immediately with investigation_id for WebSocket subscription

    The investigation runs asynchronously. Subscribe to:
    ws://.../ws/investigations/{investigation_id} for real-time events.
    """
    trace_id = getattr(request.state, "trace_id", "")
    investigation_id = str(uuid.uuid4())

    state = IncidentState(
        investigation_id=investigation_id,
        incident_id=req.incident_id,
        trace_id=trace_id,
        status=InvestigationStatus.PENDING,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store = get_state_store()
    await store.put_state(state)

    # Dispatch investigation to agent (non-blocking)
    asyncio.create_task(
        _dispatch_investigation(investigation_id, req.incident_id, trace_id, actor)
    )

    return {
        "investigation_id": investigation_id,
        "incident_id": req.incident_id,
        "status": "pending",
        "ws_url": f"/ws/investigations/{investigation_id}",
        "trace_id": trace_id,
    }


async def _dispatch_investigation(
    investigation_id: str,
    incident_id: str,
    trace_id: str,
    actor: ActorContext,
) -> None:
    """Run the investigation in a thread pool and emit events."""
    from agui.event_bus import get_bus
    from agui.schemas.events import AGUIEvent, EventType
    from agui.schemas.incidents import InvestigationStatus

    bus = get_bus()
    store = get_state_store()

    # Update status to running
    state = await store.get_state(investigation_id)
    if state:
        state.status = InvestigationStatus.RUNNING
        await store.put_state(state)

    loop = asyncio.get_event_loop()
    try:
        # Run the synchronous agent in a thread pool (via harness for self-correction)
        def run_agent():
            try:
                from supervisor.agent_harness import run_with_harness
                return run_with_harness(incident_id, investigation_id=investigation_id)
            except Exception as e:
                logger.error("Agent investigation failed: %s", e)
                raise

        result = await loop.run_in_executor(None, run_agent)

        # Update final state
        state = await store.get_state(investigation_id)
        if state:
            state.status = InvestigationStatus.COMPLETED
            state.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state.root_cause = result.get("root_cause", "") if result else ""
            state.confidence = float(result.get("confidence", 0.0)) if result else 0.0
            # Lift the R1/R2 operator-intelligence signals off the real result so
            # Operational Intelligence can consume them (no engine re-run, no
            # duplication). Absent signals stay absent — never fabricated.
            if result:
                cv = result.get("_corpus_version")
                if cv:
                    state.corpus_version = str(cv)
                el = result.get("_evidence_lifecycle")
                if isinstance(el, dict):
                    state.evidence_lifecycle = el
            state.replay_available = True
            await store.put_state(state)

    except Exception as e:
        logger.error("Investigation %s failed: %s", investigation_id, e)
        state = await store.get_state(investigation_id)
        if state:
            state.status = InvestigationStatus.FAILED
            await store.put_state(state)
        # Emit failure event
        await bus.publish(AGUIEvent(
            event_type=EventType.INVESTIGATION_FAILED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=9999,
            payload={"error": str(e)},
        ))


@router.get("")
async def list_investigations(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: ActorContext = Depends(get_actor),
):
    """List investigations with optional status filter."""
    store = get_state_store()
    investigations = await store.list_investigations(
        status=status, limit=limit, offset=offset
    )
    return {
        "investigations": [i.model_dump() for i in investigations],
        "total": len(investigations),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{investigation_id}")
async def get_investigation(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Get full investigation state."""
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation {investigation_id} not found",
        )
    return state.model_dump()


@router.get("/{investigation_id}/graph")
async def get_execution_graph(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
) -> dict:
    """
    Get the execution DAG for an investigation.

    Returns the current graph state (may be partial for running investigations).
    For completed investigations, returns full DAG with layout positions.
    """
    # Try in-memory builder first (live investigation)
    builder = get_builder(investigation_id)
    if builder:
        return builder.graph.model_dump()

    # Fall back to rebuilding from stored events
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Investigation {investigation_id} not found",
        )

    events = await store.get_events(investigation_id)
    if not events:
        # Return empty graph
        return ExecutionGraph(
            investigation_id=investigation_id,
            incident_id=state.incident_id,
            trace_id=state.trace_id,
        ).model_dump()

    graph = rebuild_from_events(
        investigation_id, state.incident_id, state.trace_id, events
    )
    return graph.model_dump()


@router.get("/{investigation_id}/risk")
async def get_risk_confidence(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
) -> RiskResponse:
    """Get current risk and confidence assessment."""
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Investigation not found")

    return RiskResponse(
        investigation_id=investigation_id,
        confidence=state.confidence,
        risk_level=state.risk_level,
        stale_sources=state.stale_sources,
        budget_used=state.budget_used,
        budget_max=state.budget_max,
        budget_pct=state.budget_pct,
        judge_scores=state.judge_scores,
        data_freshness=state.data_freshness,
        is_stale=state.is_stale,
    )


# ── Fix endpoints ─────────────────────────────────────────────────────────────


class ApplyFixRequest(BaseModel):
    action: str = "apply"           # apply | reject
    reason: str = ""                # rejection reason (if action=reject)


@router.get("/{investigation_id}/fix")
async def get_fix_proposal(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Get the proposed fix for an investigation (if any).

    Returns the fix proposal generated by the AI diff analysis, including:
    - fix_type: rollback | code_fix | none
    - fix_description: human-readable explanation
    - immediate_action: kubectl rollback command
    - permanent_action: GitHub PR details
    - confidence, risk_level, status
    """
    from supervisor.fix_engine import get_fix_engine
    engine = get_fix_engine()
    fix_status = engine.get_status(investigation_id)
    if fix_status.get("status") == "no_fix":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No proposed fix for investigation {investigation_id}",
        )
    return fix_status


@router.post("/{investigation_id}/fix")
async def apply_or_reject_fix(
    investigation_id: str,
    req: ApplyFixRequest,
    actor: ActorContext = Depends(require_role("operator")),
):
    """Approve/reject and optionally apply a proposed fix.

    Actions:
    - apply:  Approve and immediately apply the fix (rollback or PR creation)
    - reject: Reject the fix (no action taken)

    Requires operator role. All actions are audited with actor_id.
    """
    from supervisor.fix_engine import get_fix_engine
    engine = get_fix_engine()
    fix = engine.get_fix(investigation_id)

    if not fix:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No proposed fix for investigation {investigation_id}",
        )

    if req.action == "reject":
        success = engine.reject(investigation_id, actor.actor_id, req.reason)
        return {
            "investigation_id": investigation_id,
            "action": "rejected",
            "actor_id": actor.actor_id,
            "reason": req.reason,
            "success": success,
        }

    if req.action == "apply":
        # Approve first
        approved = engine.approve(investigation_id, actor.actor_id)
        if not approved:
            current_status = engine.get_status(investigation_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot approve fix in status: {current_status.get('status')}",
            )

        # Apply the fix using workers from a new supervisor instance
        try:
            from supervisor.agent import SentinalAISupervisor
            supervisor = SentinalAISupervisor()
            devops_worker = supervisor.workers["devops_worker"]
            itsm_worker = supervisor.workers["itsm_worker"]

            application = await engine.apply_fix(
                investigation_id=investigation_id,
                actor_id=actor.actor_id,
                devops_worker=devops_worker,
                itsm_worker=itsm_worker,
                incident_id=fix.incident_id,
            )

            # Start verification loop in background if fix was applied successfully
            if application.success:
                asyncio.create_task(
                    _run_verification_loop(
                        investigation_id=investigation_id,
                        incident_id=fix.incident_id,
                        service=fix.immediate_action.get("service", ""),
                        supervisor=supervisor,
                    )
                )

            return {
                "investigation_id": investigation_id,
                "action": "applied",
                "actor_id": actor.actor_id,
                "fix_id": application.fix_id,
                "action_taken": application.action_taken,
                "success": application.success,
                "result": application.result_detail,
                "verification_started": application.success,
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        except Exception as exc:
            logger.error("Fix application error for %s: %s", investigation_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Fix application failed: {exc}",
            )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown action: {req.action}. Use 'apply' or 'reject'.",
    )


@router.get("/{investigation_id}/fix/status")
async def get_fix_status(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Get live fix + verification status for an investigation.

    Returns current fix status including:
    - status: proposed | approved | applying | applied | verifying | verified | failed
    - verification progress (polls completed, stable readings)
    - ticket_closed: whether the SNOW ticket was auto-closed
    """
    from supervisor.fix_engine import get_fix_engine
    engine = get_fix_engine()
    fix_status = engine.get_status(investigation_id)
    if fix_status.get("status") == "no_fix":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No fix record for investigation {investigation_id}",
        )
    return fix_status


async def _run_verification_loop(
    investigation_id: str,
    incident_id: str,
    service: str,
    supervisor: Any,
) -> None:
    """Background task: run verification loop after fix is applied."""
    from supervisor.fix_engine import get_fix_engine, FixStatus
    from supervisor.verification_loop import VerificationLoop
    from agui.event_bus import get_bus
    from agui.schemas.events import AGUIEvent, EventType

    engine = get_fix_engine()
    fix = engine.get_fix(investigation_id)
    if not fix:
        return

    fix.status = FixStatus.VERIFYING

    metrics_worker = supervisor.workers.get("metrics_worker")
    log_worker = supervisor.workers.get("log_worker")
    itsm_worker = supervisor.workers.get("itsm_worker")

    if not metrics_worker or not log_worker:
        logger.warning("Verification loop skipped: missing workers")
        return

    # Wire callback to AGUI event bus
    bus = get_bus()

    async def _on_verification_event(inv_id: str, event_type: str, data: dict) -> None:
        try:
            await bus.publish(AGUIEvent(
                event_type=EventType.CUSTOM,
                investigation_id=inv_id,
                incident_id=incident_id,
                trace_id="",
                sequence_num=0,
                payload={"verification_event": event_type, **data},
            ))
        except Exception as exc:
            logger.warning("Verification event publish failed: %s", exc)

    loop = VerificationLoop(
        metrics_worker=metrics_worker,
        log_worker=log_worker,
        # Faster polling in staging/test environments
        poll_interval_sec=int(
            __import__("os").environ.get("VERIFICATION_POLL_INTERVAL_SEC", "60")
        ),
    )

    result = await loop.watch(
        investigation_id=investigation_id,
        service=service or "unknown",
        callback=_on_verification_event,
        itsm_worker=itsm_worker,
        incident_id=incident_id,
    )

    if result.success:
        engine.mark_verified(investigation_id, result.to_dict())
    else:
        engine.mark_failed_verification(investigation_id, result.failure_reason)
