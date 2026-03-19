"""AG UI Incidents API.

Routes:
  GET /api/v1/incidents        → List incidents (paginated, filterable)
  GET /api/v1/incidents/{id}   → Get incident with all investigations
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from agui.middleware.auth import ActorContext, get_actor
from agui.state_store import get_state_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


@router.get("")
async def list_incidents(
    severity: Optional[str] = Query(None),
    service: Optional[str] = Query(None),
    incident_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor: ActorContext = Depends(get_actor),
):
    """
    List incidents with metadata filtering.

    Supports filtering by:
    - severity (critical | major | warning | minor | info)
    - service (affected_service field)
    - incident_type (timeout | oomkill | error_spike | etc.)
    - status (investigation status)
    """
    store = get_state_store()
    investigations = await store.list_investigations(status=status, limit=limit, offset=offset)

    # Apply additional filters
    if severity:
        investigations = [i for i in investigations if i.severity == severity]
    if service:
        investigations = [
            i for i in investigations
            if service.lower() in i.affected_service.lower()
        ]
    if incident_type:
        investigations = [i for i in investigations if i.incident_type == incident_type]

    return {
        "incidents": [
            {
                "incident_id": i.incident_id,
                "investigation_id": i.investigation_id,
                "summary": i.summary,
                "affected_service": i.affected_service,
                "severity": i.severity,
                "incident_type": i.incident_type,
                "status": i.status.value,
                "confidence": i.confidence,
                "risk_level": i.risk_level,
                "started_at": i.started_at,
                "completed_at": i.completed_at,
                "duration_ms": i.duration_ms,
                "trace_id": i.trace_id,
            }
            for i in investigations
        ],
        "total": len(investigations),
        "limit": limit,
        "offset": offset,
        "filters": {
            "severity": severity,
            "service": service,
            "incident_type": incident_type,
            "status": status,
        },
    }


@router.get("/{incident_id}")
async def get_incident(
    incident_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """Get incident detail with all associated investigations."""
    store = get_state_store()
    # Get all investigations for this incident
    all_investigations = await store.list_investigations(limit=200)
    incident_investigations = [
        i for i in all_investigations
        if i.incident_id == incident_id
    ]

    if not incident_investigations:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

    # Most recent investigation is primary
    primary = sorted(
        incident_investigations,
        key=lambda i: i.started_at or "",
        reverse=True,
    )[0]

    return {
        "incident_id": incident_id,
        "investigations": [i.model_dump() for i in incident_investigations],
        "primary_investigation": primary.model_dump(),
        "investigation_count": len(incident_investigations),
    }
