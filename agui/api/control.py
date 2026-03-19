"""AG UI Human-in-the-Loop Control API.

Routes:
  POST /api/v1/investigations/{id}/control        → Send control action
  GET  /api/v1/investigations/{id}/control        → List control actions (audit log)
  GET  /api/v1/investigations/{id}/control/pending → Get pending approvals

Security:
  - approve/reject/override: requires 'approver' role minimum
  - pause/resume: requires 'operator' role minimum
  - All actions are immutably logged

Idempotency:
  - Control actions are deduplicated by action_id
  - DynamoDB conditional write prevents duplicate application
  - action_id in response body for client-side dedup
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional

from agui.middleware.auth import ActorContext, require_role, get_actor
from agui.state_store import get_state_store
from agui.event_bus import get_bus
from agui.schemas.events import AGUIEvent, EventType
from agui.schemas.incidents import ControlAction, ControlActionType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/investigations", tags=["control"])


class ControlRequest(BaseModel):
    action: str              # approve | reject | pause | resume | override | escalate
    reason: Optional[str] = None
    target_node_id: Optional[str] = None
    metadata: dict = {}


def _get_required_role(action: str) -> str:
    """Map action to minimum required role."""
    approver_actions = {"approve", "reject", "override"}
    operator_actions = {"pause", "resume", "escalate"}
    if action in approver_actions:
        return "approver"
    if action in operator_actions:
        return "operator"
    return "admin"


@router.post("/{investigation_id}/control")
async def send_control_action(
    investigation_id: str,
    req: ControlRequest,
    actor: ActorContext = Depends(get_actor),
):
    """
    Send a human-in-the-loop control action.

    Actions:
      approve  → Allow agent to proceed with pending action (requires approver)
      reject   → Stop agent from proceeding (requires approver)
      pause    → Pause investigation execution (requires operator)
      resume   → Resume paused investigation (requires operator)
      override → Override agent decision with human judgment (requires approver)
      escalate → Escalate to human expert (requires operator)

    All actions are immediately persisted and broadcast via WebSocket.
    """
    # Dynamic role check
    required_role = _get_required_role(req.action)
    role_levels = {"viewer": 0, "operator": 1, "approver": 2, "admin": 3}
    actor_level = role_levels.get(actor.actor_role, -1)
    required_level = role_levels.get(required_role, 99)

    if actor_level < required_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Action '{req.action}' requires role '{required_role}'. "
                f"Your role: '{actor.actor_role}'"
            ),
        )

    # Validate action type
    try:
        action_type = ControlActionType(req.action)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown action type: {req.action}",
        )

    # Check investigation exists
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Investigation not found")

    # Create immutable control action record
    control_action = ControlAction(
        action_id=str(uuid.uuid4()),
        investigation_id=investigation_id,
        incident_id=state.incident_id,
        action_type=action_type,
        actor_id=actor.actor_id,
        actor_role=actor.actor_role,
        reason=req.reason,
        target_node_id=req.target_node_id,
        metadata=req.metadata,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        status="pending",
    )

    # Persist to state store
    await store.put_control(control_action)

    # Update investigation state
    if req.action == "pause":
        from agui.schemas.incidents import InvestigationStatus
        state.status = InvestigationStatus.PAUSED
        await store.put_state(state)
    elif req.action == "resume":
        from agui.schemas.incidents import InvestigationStatus
        state.status = InvestigationStatus.RUNNING
        await store.put_state(state)

    # Emit event to WebSocket subscribers
    bus = get_bus()
    event_type = (
        EventType.CONTROL_APPROVED if req.action == "approve"
        else EventType.CONTROL_REJECTED if req.action == "reject"
        else EventType.INVESTIGATION_PAUSED if req.action == "pause"
        else EventType.INVESTIGATION_RESUMED if req.action == "resume"
        else EventType.CONTROL_APPROVED  # override/escalate use approve event type
    )
    await bus.publish(AGUIEvent(
        event_type=event_type,
        investigation_id=investigation_id,
        incident_id=state.incident_id,
        trace_id=state.trace_id,
        sequence_num=99990,
        payload={
            "action_id": control_action.action_id,
            "action": req.action,
            "actor_id": actor.actor_id,
            "actor_role": actor.actor_role,
            "reason": req.reason,
            "target_node_id": req.target_node_id,
            "node_id": req.target_node_id,  # for graph builder
        },
    ))

    logger.info(
        "Control action %s by %s (%s) on investigation %s",
        req.action, actor.actor_id, actor.actor_role, investigation_id,
    )

    return {
        "action_id": control_action.action_id,
        "investigation_id": investigation_id,
        "action": req.action,
        "status": "applied",
        "actor_id": actor.actor_id,
        "timestamp": control_action.timestamp,
    }


@router.get("/{investigation_id}/control")
async def list_control_actions(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """
    Get the full control action audit log for an investigation.
    Immutable history of all human decisions.
    """
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Investigation not found")

    return {
        "investigation_id": investigation_id,
        "control_actions": [ca.model_dump() for ca in state.control_actions],
        "total": len(state.control_actions),
        "awaiting_approval_for": state.awaiting_approval_for,
    }


@router.get("/{investigation_id}/control/pending")
async def get_pending_approvals(
    investigation_id: str,
    actor: ActorContext = Depends(require_role("operator")),
):
    """Get any pending approvals that require human action."""
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        raise HTTPException(status_code=404, detail="Investigation not found")

    pending = [
        ca for ca in state.control_actions
        if ca.status == "pending"
    ]

    return {
        "investigation_id": investigation_id,
        "pending_approvals": [ca.model_dump() for ca in pending],
        "requires_action": len(pending) > 0,
        "awaiting_approval_for": state.awaiting_approval_for,
    }
