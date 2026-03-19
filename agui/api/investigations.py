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
from typing import Optional

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
    import concurrent.futures
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
        # Run the synchronous agent in a thread pool
        def run_agent():
            try:
                from supervisor.agent import investigate
                from supervisor.agui_bridge import bridge
                # Inject investigation_id into the bridge context
                return investigate(incident_id, investigation_id=investigation_id)
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
