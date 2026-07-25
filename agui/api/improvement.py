"""AG UI Operational Improvement API — telemetry → ranked backlog.

Route:
  GET /api/v1/improvement-report → the ROI-ranked, evidence-backed improvement
      backlog derived from recorded operator events. Returns NOT_MEASURED when
      there is not enough pilot data (the honest default today).

Consumes existing artifacts only (the operator-telemetry event log). Modifies
nothing; invents nothing.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Query

from agui.improvement_engine import analyze
from agui.middleware.auth import ActorContext, get_actor
from agui.operator_telemetry import load_events

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["improvement"])


def _events_path() -> str:
    return os.environ.get("AGUI_OPERATOR_EVENTS_PATH",
                          "/tmp/agui-operator-events.jsonl")


@router.get("/improvement-report")
async def get_improvement_report(
    min_sessions: int = Query(5, ge=1, le=1000),
    actor: ActorContext = Depends(get_actor),
):
    """Ranked improvement backlog from observed operator behavior. With no
    pilot data this returns status NOT_MEASURED — findings are never
    fabricated."""
    events = load_events(_events_path())
    return analyze(events, min_sessions=min_sessions)
