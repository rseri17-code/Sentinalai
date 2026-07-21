"""AG UI Operator Telemetry API — record and measure the operator timeline.

Routes:
  POST /api/v1/investigations/{id}/operator-events → record one operator
       milestone (investigation_opened, evidence_panel_opened, confidence_viewed,
       recommendation_accepted, external_tool_opened, …). Append-only.
  GET  /api/v1/investigations/{id}/operator-mtti    → operator timeline +
       external-tool escapes + decision quality for that investigation.

Reuses the pilot-telemetry append-only store and the operator_telemetry model.
No new framework, no synthesized interactions, timestamps supplied by the UI.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Body, Depends

from agui.middleware.auth import ActorContext, get_actor
from agui import operator_telemetry as ot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/investigations", tags=["operator-telemetry"])


def _events_path() -> str:
    return os.environ.get("AGUI_OPERATOR_EVENTS_PATH",
                          "/tmp/agui-operator-events.jsonl")


def _events_for(investigation_id: str) -> list[dict]:
    return [e for e in ot.load_events(_events_path())
            if str(e.get("incident_id", "")) == investigation_id]


@router.post("/{investigation_id}/operator-events")
async def record_operator_event(
    investigation_id: str,
    body: dict = Body(...),
    actor: ActorContext = Depends(get_actor),
):
    """Record one operator milestone. ``at`` is the UI's real interaction time
    (epoch ms). The milestone must be a known operator milestone."""
    event = ot.operator_event(
        str(body.get("milestone", "")),
        at=int(body.get("at", 0)),
        operator=str(getattr(actor, "user_id", "") or body.get("operator", "")),
        investigation_id=investigation_id,
        service=str(body.get("service", "")),
        application=str(body.get("application", "")),
        entity=str(body.get("entity", "")),
        screen=str(body.get("screen", "")),
        duration_ms=body.get("duration_ms"),
        tool_name=str(body.get("tool_name", "")),
        reason=str(body.get("reason", "")),
        time_away_ms=body.get("time_away_ms"),
    )
    ot.append_event(_events_path(), event)
    return {"recorded": True, "event_id": event["event_id"]}


@router.get("/{investigation_id}/operator-mtti")
async def get_operator_mtti(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Operator timeline (kept separate from system MTTI) + escape analysis +
    decision quality for one investigation."""
    events = _events_for(investigation_id)
    payload = ot.compute_operator_mtti(events)
    payload["investigation_id"] = investigation_id
    payload["external_tool_escapes"] = ot.external_tool_escapes(events)
    payload["decision_quality"] = ot.decision_quality(events)
    return payload
