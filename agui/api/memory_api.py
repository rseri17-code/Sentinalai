"""Episodic Memory and Resolution Knowledge API.

Routes:
  GET  /api/memory/episodes              → list/search episodes
  GET  /api/memory/similar               → similarity search
  GET  /api/memory/service-summary/{id}  → per-service stats
  GET  /api/memory/recommend             → resolution recommendations
  GET  /api/memory/resolution-leaderboard → top actions for incident type
  POST /api/memory/episode               → record a new episode
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["episodic-memory"])


def _get_episodic_memory():
    from intelligence.episodic_memory import EpisodicMemory
    return EpisodicMemory()


def _get_resolution_knowledge():
    from intelligence.resolution_knowledge import ResolutionKnowledge
    return ResolutionKnowledge()


@router.get("/episodes")
async def list_episodes(
    service: Optional[str] = Query(None),
    incident_type: Optional[str] = Query(None),
    limit: int = Query(5, ge=1, le=100),
):
    """Search episodes by service and/or incident type."""
    try:
        mem = _get_episodic_memory()
        episodes = mem.search(service=service, incident_type=incident_type, limit=limit)
        return {
            "episodes": [e.to_dict() for e in episodes],
            "count": len(episodes),
            "filters": {"service": service, "incident_type": incident_type, "limit": limit},
        }
    except Exception as exc:
        logger.warning("GET /api/memory/episodes error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.get("/similar")
async def get_similar(
    signature: str = Query(..., description="Failure signature to match against"),
    service: Optional[str] = Query(None),
    limit: int = Query(3, ge=1, le=20),
):
    """Return episodes most similar to the given failure signature."""
    try:
        mem = _get_episodic_memory()
        episodes = mem.get_similar(failure_signature=signature, service=service, limit=limit)
        return {
            "similar_episodes": [e.to_dict() for e in episodes],
            "count": len(episodes),
            "query": {"signature": signature, "service": service, "limit": limit},
        }
    except Exception as exc:
        logger.warning("GET /api/memory/similar error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.get("/service-summary/{service_id}")
async def service_summary(service_id: str):
    """Return aggregated incident statistics for a service."""
    try:
        mem = _get_episodic_memory()
        summary = mem.summary_for_service(service_id)
        return {"service": service_id, "summary": summary}
    except Exception as exc:
        logger.warning("GET /api/memory/service-summary error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.get("/recommend")
async def recommend(
    failure_mode: str = Query(...),
    incident_type: str = Query(...),
    tier: int = Query(2, ge=1, le=3),
):
    """Return top resolution recommendations for a failure mode."""
    try:
        rk = _get_resolution_knowledge()
        recs = rk.recommend(failure_mode=failure_mode, incident_type=incident_type, service_tier=tier)
        return {
            "recommendations": [r.to_dict() for r in recs],
            "count": len(recs),
            "query": {"failure_mode": failure_mode, "incident_type": incident_type, "tier": tier},
        }
    except Exception as exc:
        logger.warning("GET /api/memory/recommend error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.get("/resolution-leaderboard")
async def resolution_leaderboard(
    incident_type: str = Query(...),
):
    """Return action leaderboard for an incident type sorted by success rate."""
    try:
        rk = _get_resolution_knowledge()
        leaderboard = rk.get_leaderboard(incident_type=incident_type)
        return {
            "incident_type": incident_type,
            "leaderboard": leaderboard,
            "count": len(leaderboard),
        }
    except Exception as exc:
        logger.warning("GET /api/memory/resolution-leaderboard error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})


@router.post("/episode")
async def record_episode(body: dict):
    """Record a new investigation episode."""
    try:
        from intelligence.episodic_memory import Episode, EpisodicMemory
        episode = Episode(
            episode_id=body.get("episode_id") or str(uuid.uuid4()),
            incident_id=body.get("incident_id", ""),
            service=body.get("service", ""),
            incident_type=body.get("incident_type", ""),
            failure_signature=body.get("failure_signature", ""),
            root_cause=body.get("root_cause", ""),
            confidence=float(body.get("confidence", 0.0)),
            resolution_action=body.get("resolution_action", ""),
            resolved_by=body.get("resolved_by", "unknown"),
            time_to_resolve_ms=int(body.get("time_to_resolve_ms", 0)),
            evidence_keys=list(body.get("evidence_keys", [])),
            outcome=body.get("outcome", "unknown"),
            tags=list(body.get("tags", [])),
            recorded_at=body.get("recorded_at") or datetime.now(timezone.utc).isoformat(),
        )
        mem = EpisodicMemory()
        mem.record(episode)
        return {"ok": True, "episode_id": episode.episode_id}
    except Exception as exc:
        logger.warning("POST /api/memory/episode error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": str(exc)})
