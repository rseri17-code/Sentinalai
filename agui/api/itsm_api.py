"""ITSM Write-back API — REST endpoints for bi-directional ITSM integration.

Endpoints:
  POST /api/itsm/resolve      — resolve an incident with root cause + action
  POST /api/itsm/acknowledge  — acknowledge an incident
  GET  /api/itsm/status       — returns engine config (enabled, provider, dry_run)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("sentinalai.api.itsm")

router = APIRouter(prefix="/api/itsm", tags=["itsm"])


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class ResolveRequest(BaseModel):
    incident_id: str
    service: str
    root_cause: str
    resolution_action: str
    confidence: float
    runbook_url: str = ""


class AcknowledgeRequest(BaseModel):
    incident_id: str
    service: str
    confidence: float


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/resolve")
async def itsm_resolve(body: ResolveRequest) -> dict:
    """Resolve an incident in the configured ITSM provider."""
    from intelligence.itsm_writebacks import get_engine
    engine = get_engine()
    result = engine.resolve(
        incident_id=body.incident_id,
        service=body.service,
        root_cause=body.root_cause,
        resolution_action=body.resolution_action,
        confidence=body.confidence,
        runbook_url=body.runbook_url,
    )
    return {
        "incident_id": result.incident_id,
        "provider": result.provider,
        "action": result.action,
        "success": result.success,
        "message": result.message,
        "dry_run": result.dry_run,
    }


@router.post("/acknowledge")
async def itsm_acknowledge(body: AcknowledgeRequest) -> dict:
    """Acknowledge an incident in the configured ITSM provider."""
    from intelligence.itsm_writebacks import get_engine
    engine = get_engine()
    result = engine.acknowledge(
        incident_id=body.incident_id,
        service=body.service,
        confidence=body.confidence,
    )
    return {
        "incident_id": result.incident_id,
        "provider": result.provider,
        "action": result.action,
        "success": result.success,
        "message": result.message,
        "dry_run": result.dry_run,
    }


@router.get("/status")
async def itsm_status() -> dict:
    """Return current ITSM engine configuration."""
    from intelligence.itsm_writebacks import get_engine
    import os
    engine = get_engine()
    enabled = os.environ.get("ITSM_WRITEBACK_ENABLED", "false").lower() in ("true", "1", "yes")
    return {
        "enabled": enabled,
        "provider": engine.provider,
        "dry_run": engine.dry_run,
    }
