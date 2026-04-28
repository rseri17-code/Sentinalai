"""Postmortem API — generate, retrieve, approve, and publish blameless postmortems.

POST /api/v1/postmortem                           — Generate postmortem from RCA
GET  /api/v1/postmortem/{report_id}               — Retrieve a postmortem
POST /api/v1/postmortem/{report_id}/approve       — Approve (status: draft → approved)
POST /api/v1/postmortem/{report_id}/publish       — Publish to Confluence
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import time
import uuid

logger = logging.getLogger("sentinalai.agui.postmortem")

router = APIRouter(prefix="/api/v1/postmortem", tags=["postmortem"])

# In-memory store (replace with DB persistence in production)
_POSTMORTEMS: dict[str, Any] = {}
# Comments store: report_id → list of comment dicts
_COMMENTS: dict[str, list[dict]] = {}


@router.post("")
async def generate_postmortem(body: dict = {}) -> JSONResponse:
    """Generate a blameless postmortem from an RCA result."""
    incident_id = body.get("incident_id") or body.get("investigation_id") or "UNKNOWN"

    # Fetch the RCA result for this incident
    rca_result = await _fetch_rca_result(incident_id)

    try:
        from supervisor.postmortem_generator import generate_postmortem as _gen

        resolved_at = body.get("resolved_at", datetime.now(timezone.utc).isoformat())
        team_notes = body.get("team_notes", [])
        similar_incidents = body.get("similar_incidents", [])

        report = _gen(
            rca_result=rca_result,
            resolved_at=resolved_at,
            team_notes=team_notes,
            similar_incidents=similar_incidents,
        )

        report_dict = _report_to_dict(report)
        _POSTMORTEMS[report.report_id] = report
        return JSONResponse(report_dict)

    except Exception as exc:
        logger.exception("Failed to generate postmortem for %s: %s", incident_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/{report_id}")
async def get_postmortem(report_id: str) -> JSONResponse:
    """Retrieve a generated postmortem by report ID."""
    report = _POSTMORTEMS.get(report_id)
    if report is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(_report_to_dict(report))


@router.post("/{report_id}/approve")
async def approve_postmortem(report_id: str, body: dict = {}) -> JSONResponse:
    """Approve a postmortem draft (human gate — required before publishing)."""
    report = _POSTMORTEMS.get(report_id)
    if report is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    reviewer = body.get("reviewer", "operator")
    report.approve(reviewer)
    logger.info("Postmortem %s approved by %s", report_id, reviewer)
    return JSONResponse({"ok": True, "status": "approved", "reviewed_by": reviewer})


@router.post("/{report_id}/publish")
async def publish_postmortem(report_id: str, body: dict = {}) -> JSONResponse:
    """Publish an approved postmortem to Confluence."""
    report = _POSTMORTEMS.get(report_id)
    if report is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    if report.status != "approved":
        return JSONResponse(
            {"error": "not_approved", "detail": "Postmortem must be approved before publishing"},
            status_code=409,
        )

    try:
        from workers.confluence_worker import ConfluenceWorker

        worker = ConfluenceWorker()
        markdown = report.to_markdown()
        title = f"Postmortem — {report.incident_id} — {report.affected_service}"

        result = worker.execute(
            "create_page",
            {
                "title": title,
                "content": markdown,
                "space": "SRE",
                "labels": ["postmortem", report.affected_service, "sentinalai"],
            },
        )
        confluence_url = result.get("url", "")
        logger.info("Postmortem %s published to Confluence: %s", report_id, confluence_url)
        return JSONResponse({"ok": True, "confluence_url": confluence_url})

    except Exception as exc:
        logger.warning("Confluence publish not available: %s", exc)
        return JSONResponse(
            {"ok": False, "confluence_url": "", "error": str(exc)},
            status_code=200,
        )


@router.get("/{report_id}/comments")
async def list_comments(report_id: str) -> JSONResponse:
    """List all comments on a postmortem."""
    if report_id not in _POSTMORTEMS:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"comments": _COMMENTS.get(report_id, [])})


@router.post("/{report_id}/comments")
async def add_comment(report_id: str, body: dict = {}) -> JSONResponse:
    """Add a collaboration comment to a postmortem.

    Broadcasts a postmortem.comment_added event to all WebSocket subscribers
    watching the related investigation.

    Body: { author: str, text: str, investigation_id?: str }
    """
    if report_id not in _POSTMORTEMS:
        return JSONResponse({"error": "not_found"}, status_code=404)

    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)

    comment = {
        "comment_id": str(uuid.uuid4()),
        "report_id": report_id,
        "author": body.get("author", "anonymous"),
        "text": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ts": time.time(),
    }
    _COMMENTS.setdefault(report_id, []).append(comment)
    logger.info("Comment added to postmortem %s by %s", report_id, comment["author"])

    # Broadcast via WebSocket event bus (non-blocking; bus may not be running)
    investigation_id = body.get("investigation_id", "")
    if investigation_id:
        try:
            from agui.event_bus import get_bus
            from agui.schemas.events import AGUIEvent, EventType
            bus = get_bus()
            event = AGUIEvent(
                investigation_id=investigation_id,
                event_type=EventType.POSTMORTEM_COMMENT_ADDED,
                payload={
                    "report_id": report_id,
                    "comment": comment,
                },
            )
            await bus.publish(event)
        except Exception as exc:
            logger.debug("WebSocket broadcast skipped: %s", exc)

    return JSONResponse({"ok": True, "comment": comment}, status_code=201)


async def _fetch_rca_result(incident_id: str) -> dict:
    """Fetch the RCA result for an incident from the investigation store."""
    try:
        from database.persistence import load_investigation
        result = load_investigation(incident_id)
        if result:
            return result
    except Exception:
        pass

    try:
        from supervisor.experience_store import ExperienceStore
        store = ExperienceStore()
        experiences = store.retrieve_similar(incident_type="", service="")
        for exp in experiences:
            if exp.get("incident_id") == incident_id:
                return exp
    except Exception:
        pass

    # Minimal fallback — postmortem generator handles empty RCA gracefully
    return {
        "incident_id": incident_id,
        "incident_type": "unknown",
        "affected_service": "unknown",
        "severity_label": "High",
        "root_cause": "Under investigation",
        "confidence": 0,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "evidence_timeline": [],
    }


def _report_to_dict(report: Any) -> dict:
    """Convert PostmortemReport to JSON-serialisable dict."""
    try:
        d = asdict(report)
        # Convert ActionItem objects if not already dicts
        if "action_items" in d:
            d["action_items"] = [
                ai if isinstance(ai, dict) else asdict(ai)
                for ai in d["action_items"]
            ]
        return d
    except Exception:
        return {
            "report_id": getattr(report, "report_id", "unknown"),
            "incident_id": getattr(report, "incident_id", "unknown"),
            "status": getattr(report, "status", "draft"),
            "reviewed_by": getattr(report, "reviewed_by", None),
            "generated_at": getattr(report, "generated_at", ""),
            "severity": getattr(report, "severity", "?"),
            "affected_service": getattr(report, "affected_service", "?"),
            "duration_minutes": getattr(report, "duration_minutes", 0),
            "executive_summary": getattr(report, "executive_summary", ""),
            "impact_statement": getattr(report, "impact_statement", ""),
            "timeline": getattr(report, "timeline", []),
            "contributing_factors": getattr(report, "contributing_factors", []),
            "what_went_well": getattr(report, "what_went_well", []),
            "what_needs_improvement": getattr(report, "what_needs_improvement", []),
            "five_whys": getattr(report, "five_whys", []),
            "action_items": [
                {
                    "title": getattr(ai, "title", "?"),
                    "description": getattr(ai, "description", ""),
                    "priority": getattr(ai, "priority", "P3"),
                    "category": getattr(ai, "category", "prevention"),
                    "owner": getattr(ai, "owner", "sre-team"),
                    "due_days": getattr(ai, "due_days", 30),
                    "estimated_effort": getattr(ai, "estimated_effort", "days"),
                }
                for ai in getattr(report, "action_items", [])
            ],
            "prevention_recommendations": getattr(report, "prevention_recommendations", []),
            "similar_past_incidents": getattr(report, "similar_past_incidents", []),
        }
