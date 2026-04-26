"""Shift Handoff API — generate and retrieve shift intelligence briefs.

POST /api/v1/handoff         — Generate a shift handoff brief
GET  /api/v1/handoff/current — Get the most recently generated brief
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("sentinalai.agui.handoff")

router = APIRouter(prefix="/api/v1/handoff", tags=["handoff"])

_CURRENT_BRIEF: dict | None = None


@router.post("")
async def generate_handoff(body: dict = {}) -> JSONResponse:
    """Generate a shift handoff intelligence brief from recent experience data."""
    global _CURRENT_BRIEF

    outgoing = body.get("outgoing_engineer", "outgoing-sre")
    incoming = body.get("incoming_engineer", "incoming-sre")
    lookback_days = body.get("lookback_days", 7)

    try:
        from supervisor.shift_handoff import generate_handoff_brief

        # Pull recent experiences from experience store
        experiences: list[dict] = []
        try:
            from supervisor.experience_store import ExperienceStore
            store = ExperienceStore()
            experiences = store.get_all() if hasattr(store, "get_all") else []
        except Exception:
            pass

        # Pull active investigations from state store
        active_incidents: list[dict] = []
        try:
            from agui.state_store import get_state_store
            store_inst = get_state_store()
            if hasattr(store_inst, "list_active"):
                active = store_inst.list_active()
                active_incidents = [
                    {
                        "incident_id": s.incident_id,
                        "status": s.status,
                        "service": getattr(s, "affected_service", "?"),
                    }
                    for s in active
                ]
        except Exception:
            pass

        # Pull upcoming ITSM changes
        upcoming_changes: list[dict] = []
        try:
            from workers.itsm_worker import ItsmWorker
            itsm = ItsmWorker()
            result = itsm.execute("get_change_records", {"window": "8h"})
            upcoming_changes = result.get("changes", [])
        except Exception:
            pass

        brief = generate_handoff_brief(
            experiences=experiences,
            active_incidents=active_incidents,
            upcoming_changes=upcoming_changes,
            outgoing_engineer=outgoing,
            incoming_engineer=incoming,
            lookback_days=lookback_days,
        )

        brief_dict = _brief_to_dict(brief)
        _CURRENT_BRIEF = brief_dict
        return JSONResponse(brief_dict)

    except Exception as exc:
        logger.exception("Failed to generate handoff brief: %s", exc)
        # Return a minimal graceful response
        fallback = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "shift_start": datetime.now(timezone.utc).isoformat(),
            "outgoing_engineer": outgoing,
            "incoming_engineer": incoming,
            "fragile_services": [],
            "active_investigations": [],
            "watch_items": [],
            "upcoming_risk": [],
            "conditional_guidance": [],
            "open_action_items": [],
            "summary": "Handoff brief generation encountered an error — review incidents manually.",
        }
        return JSONResponse(fallback)


@router.get("/current")
async def get_current_handoff() -> JSONResponse:
    """Return the most recently generated shift handoff brief."""
    if _CURRENT_BRIEF is None:
        return JSONResponse({"error": "no_brief_generated"}, status_code=404)
    return JSONResponse(_CURRENT_BRIEF)


def _brief_to_dict(brief) -> dict:
    """Convert HandoffBrief dataclass to JSON-serialisable dict."""
    from dataclasses import asdict
    try:
        return asdict(brief)
    except Exception:
        # Fallback for non-dataclass objects
        return {
            "generated_at": getattr(brief, "generated_at", ""),
            "shift_start": getattr(brief, "shift_start", ""),
            "outgoing_engineer": getattr(brief, "outgoing_engineer", ""),
            "incoming_engineer": getattr(brief, "incoming_engineer", ""),
            "fragile_services": [
                {
                    "service": fs.service,
                    "reason": fs.reason,
                    "incident_count_7d": fs.incident_count_7d,
                    "last_incident_type": fs.last_incident_type,
                    "risk_level": fs.risk_level,
                    "watch_signals": fs.watch_signals,
                }
                for fs in getattr(brief, "fragile_services", [])
            ],
            "active_investigations": list(getattr(brief, "active_investigations", [])),
            "watch_items": list(getattr(brief, "watch_items", [])),
            "upcoming_risk": list(getattr(brief, "upcoming_risk", [])),
            "conditional_guidance": [
                {
                    "trigger": g.trigger,
                    "action": g.action,
                    "escalate_to": g.escalate_to,
                    "runbook_hint": g.runbook_hint,
                }
                for g in getattr(brief, "conditional_guidance", [])
            ],
            "open_action_items": list(getattr(brief, "open_action_items", [])),
            "summary": getattr(brief, "summary", ""),
        }
