"""AG UI Memory Trace API.

Routes:
  GET /api/v1/investigations/{id}/memory          → Get memory matches
  GET /api/v1/investigations/{id}/memory/scoring  → Get memory scoring dashboard
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from agui.middleware.auth import ActorContext, get_actor
from agui.state_store import get_state_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/investigations", tags=["memory"])


@router.get("/{investigation_id}/memory")
async def get_memory_trace(
    investigation_id: str,
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    source: Optional[str] = Query(None, description="stm | ltm | knowledge_graph"),
    service: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    actor: ActorContext = Depends(get_actor),
):
    """
    Get similar incidents from AgentCore Memory for this investigation.

    Supports filtering by:
    - min_score: minimum similarity score threshold
    - source: memory source (STM, LTM, or knowledge graph)
    - service: filter by affected service
    """
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Investigation not found")

    matches = state.memory_matches

    # Apply filters
    if min_score > 0:
        matches = [m for m in matches if m.similarity_score >= min_score]
    if source:
        matches = [m for m in matches if m.source == source]
    if service:
        matches = [
            m for m in matches
            if service.lower() in m.service.lower()
        ]

    # Sort by similarity score descending
    matches = sorted(matches, key=lambda m: m.similarity_score, reverse=True)[:limit]

    return {
        "investigation_id": investigation_id,
        "incident_id": state.incident_id,
        "affected_service": state.affected_service,
        "matches": [m.model_dump() for m in matches],
        "total": len(matches),
        "filters": {
            "min_score": min_score,
            "source": source,
            "service": service,
        },
    }


@router.get("/{investigation_id}/memory/scoring")
async def get_memory_scoring(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """
    Get the memory scoring dashboard for this investigation.

    Returns:
    - LLM judge scores (6 dimensions)
    - Confidence calibration breakdown
    - Evidence completeness score
    - Budget utilization
    - Circuit breaker states
    """
    store = get_state_store()
    state = await store.get_state(investigation_id)
    if not state:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Investigation not found")

    # Build scoring breakdown
    judge_scores = state.judge_scores
    avg_judge = (
        sum(judge_scores.values()) / len(judge_scores)
        if judge_scores else 0.0
    )

    evidence_completeness = (
        state.tool_calls_success / state.tool_calls_total
        if state.tool_calls_total > 0 else 0.0
    )

    return {
        "investigation_id": investigation_id,
        "scoring": {
            "confidence": state.confidence,
            "risk_level": state.risk_level,
            "judge_scores": judge_scores,
            "judge_average": round(avg_judge, 3),
            "evidence_completeness": round(evidence_completeness, 3),
            "hypothesis_count": len(state.hypotheses),
            "winner_hypothesis": state.winner_hypothesis,
        },
        "budget": {
            "used": state.budget_used,
            "max": state.budget_max,
            "pct": state.budget_pct,
            "tool_calls_success": state.tool_calls_success,
            "tool_calls_failed": state.tool_calls_failed,
            "tool_calls_total": state.tool_calls_total,
        },
        "hypotheses": [h.model_dump() for h in state.hypotheses],
        "data_freshness": state.data_freshness,
        "stale_sources": state.stale_sources,
    }
