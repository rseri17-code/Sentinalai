"""AG UI MTTI API — decision-acceleration timing per investigation.

Routes:
  GET /api/v1/investigations/{id}/mtti  → the MTTI timeline for one investigation
  GET /api/v1/mtti/summary              → median MTTI segments across investigations

Reads only the recorded event stream (existing runtime telemetry) and composes
``agui.mtti``. No new clock in the deterministic core; nothing fabricated —
missing milestones are null and cross-workflow baseline comparison is reported
NOT_MEASURED until a controlled pilot supplies it.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from agui.middleware.auth import ActorContext, get_actor
from agui.mtti import compute_mtti, summarize_mtti
from agui.state_store import get_state_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["mtti"])


@router.get("/investigations/{investigation_id}/mtti")
async def get_investigation_mtti(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """MTTI timeline (time to first evidence / root cause / owner / recommendation
    / completion) for a single investigation, from its recorded events."""
    store = get_state_store()
    events = await store.get_events(investigation_id)
    payload = compute_mtti([e.model_dump() if hasattr(e, "model_dump") else e
                            for e in events])
    payload["investigation_id"] = investigation_id
    return payload


@router.get("/mtti/summary")
async def get_mtti_summary(
    limit: int = Query(200, ge=1, le=1000),
    actor: ActorContext = Depends(get_actor),
):
    """Median MTTI segments across completed investigations. Baseline
    comparison stays NOT_MEASURED until a controlled pilot provides it."""
    store = get_state_store()
    states = await store.list_investigations(status="completed", limit=limit,
                                             offset=0)
    rows = []
    for s in states:
        events = await store.get_events(s.investigation_id)
        rows.append(compute_mtti([e.model_dump() if hasattr(e, "model_dump")
                                  else e for e in events]))
    return summarize_mtti(rows)
