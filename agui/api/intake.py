"""Webhook intake endpoints for external alerting systems.

Routes:
  POST /api/v1/webhooks/moogsoft    → Moogsoft alert webhook
  POST /api/v1/webhooks/pagerduty   → PagerDuty event webhook (v2/v3)
  POST /api/v1/webhooks/servicenow  → ServiceNow incident webhook
  POST /api/v1/incidents            → Manual incident submission (any format)

All endpoints:
  1. Validate optional HMAC signature
  2. Normalize to canonical Incident model
  3. Create IncidentState record in state store
  4. Dispatch investigation asynchronously
  5. Return 202 Accepted with investigation_id

Signature validation (opt-in via env vars):
  MOOGSOFT_WEBHOOK_SECRET   — shared secret for X-Moogsoft-Signature header
  PAGERDUTY_WEBHOOK_SECRET  — shared secret for X-PagerDuty-Signature header
  SNOW_WEBHOOK_SECRET       — shared secret for X-ServiceNow-Hmac header
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from agui.state_store import get_state_store
from agui.schemas.incidents import IncidentState, InvestigationStatus
from supervisor.incident_model import Incident

logger = logging.getLogger(__name__)

router = APIRouter(tags=["intake"])

# ---------------------------------------------------------------------------
# Webhook secrets (opt-in; empty string = no validation)
# ---------------------------------------------------------------------------

_MOOGSOFT_SECRET = os.environ.get("MOOGSOFT_WEBHOOK_SECRET", "")
_PAGERDUTY_SECRET = os.environ.get("PAGERDUTY_WEBHOOK_SECRET", "")
_SNOW_SECRET = os.environ.get("SNOW_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Signature validation helpers
# ---------------------------------------------------------------------------

def _verify_hmac_sha256(secret: str, body: bytes, header_value: str) -> bool:
    """Return True if the header value matches HMAC-SHA256(secret, body).

    Accepts both raw hex and 'sha256=<hex>' formats (PagerDuty style).
    """
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = header_value.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, received)


async def _check_sig(request: Request, secret: str, header_name: str) -> None:
    """Raise 401 if signature header is present but invalid.  Skip if no secret configured."""
    if not secret:
        return
    sig = request.headers.get(header_name, "")
    if not sig:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=f"Missing {header_name} header")
    body = await request.body()
    if not _verify_hmac_sha256(secret, body, sig):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Webhook signature mismatch")


# ---------------------------------------------------------------------------
# Shared investigation dispatch
# ---------------------------------------------------------------------------

async def _accept_and_dispatch(incident: Incident, trace_id: str = "") -> dict[str, Any]:
    """Persist IncidentState and kick off investigation. Returns response dict."""
    investigation_id = str(uuid.uuid4())
    state = IncidentState(
        investigation_id=investigation_id,
        incident_id=incident.incident_id,
        trace_id=trace_id,
        status=InvestigationStatus.PENDING,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store = get_state_store()
    await store.put_state(state)

    asyncio.create_task(_run_investigation(investigation_id, incident, trace_id, store))

    logger.info(
        "Webhook accepted: source=%s incident=%s investigation=%s",
        incident.source, incident.incident_id, investigation_id,
    )
    return {
        "status": "accepted",
        "investigation_id": investigation_id,
        "incident_id": incident.incident_id,
        "source": incident.source,
        "ws_url": f"/ws/investigations/{investigation_id}",
    }


async def _run_investigation(
    investigation_id: str,
    incident: Incident,
    trace_id: str,
    store: Any,
) -> None:
    """Run investigation in a thread pool, update state when done."""
    state = await store.get_state(investigation_id)
    if state:
        state.status = InvestigationStatus.RUNNING
        await store.put_state(state)

    loop = asyncio.get_event_loop()
    try:
        def _run_agent():
            from supervisor.agent import investigate
            return investigate(incident.incident_id, investigation_id=investigation_id)

        result = await loop.run_in_executor(None, _run_agent)

        state = await store.get_state(investigation_id)
        if state:
            state.status = InvestigationStatus.COMPLETED
            state.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state.root_cause = result.get("root_cause", "") if result else ""
            state.confidence = float(result.get("confidence", 0.0)) if result else 0.0
            state.replay_available = True
            await store.put_state(state)

    except Exception as exc:
        logger.error("Intake investigation %s failed: %s", investigation_id, exc)
        state = await store.get_state(investigation_id)
        if state:
            state.status = InvestigationStatus.FAILED
            await store.put_state(state)


# ---------------------------------------------------------------------------
# Moogsoft webhook
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/webhooks/moogsoft",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Moogsoft alert webhook",
)
async def moogsoft_webhook(request: Request):
    """Accept Moogsoft incident/alert webhook and trigger investigation.

    Moogsoft sends incident objects with fields:
      incident_id, summary, affected_service/service, severity (1-5 or string),
      status, created_at, description.

    Optional signature validation via MOOGSOFT_WEBHOOK_SECRET env var.
    """
    await _check_sig(request, _MOOGSOFT_SECRET, "X-Moogsoft-Signature")
    payload = await request.json()

    # Moogsoft can send a single incident or a list in "incidents"
    incidents_raw = payload if isinstance(payload, list) else payload.get("incidents", [payload])
    if not incidents_raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty payload")

    # Process only the first (or all, depending on use-case — process all in parallel)
    results = []
    for raw in incidents_raw:
        try:
            incident = Incident.from_moogsoft(raw)
        except (ValueError, KeyError) as exc:
            logger.warning("Moogsoft webhook: skipping invalid incident: %s", exc)
            continue
        results.append(await _accept_and_dispatch(incident))

    if not results:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No valid incidents in payload")
    return results[0] if len(results) == 1 else {"accepted": results}


# ---------------------------------------------------------------------------
# PagerDuty webhook
# ---------------------------------------------------------------------------

_PD_TRIGGER_EVENTS = {"incident.trigger", "incident.triggered", "incident.alert.triggered"}


@router.post(
    "/api/v1/webhooks/pagerduty",
    status_code=status.HTTP_202_ACCEPTED,
    summary="PagerDuty event webhook (v2/v3)",
)
async def pagerduty_webhook(request: Request):
    """Accept PagerDuty event webhook and trigger investigation.

    Supports both v2 (messages[].incident) and v3 (event.data) formats.
    Only trigger/alert events start an investigation; others return 200 no-op.

    Optional signature validation via PAGERDUTY_WEBHOOK_SECRET env var.
    """
    await _check_sig(request, _PAGERDUTY_SECRET, "X-PagerDuty-Signature")
    payload = await request.json()

    incidents_raw: list[dict] = []

    # v2 format: {"messages": [{"event": "incident.trigger", "incident": {...}}]}
    if "messages" in payload:
        for msg in payload["messages"]:
            event_type = msg.get("event", "")
            if event_type in _PD_TRIGGER_EVENTS:
                inc = msg.get("incident") or msg.get("data", {})
                if inc:
                    incidents_raw.append({"_pd_format": "v2", **inc})

    # v3 format: {"event": {"event_type": "incident.triggered", "data": {...}}}
    elif "event" in payload:
        event_obj = payload["event"]
        event_type = event_obj.get("event_type", "")
        if event_type in _PD_TRIGGER_EVENTS:
            inc = event_obj.get("data", {})
            if inc:
                incidents_raw.append({"_pd_format": "v3", **inc})

    # Single incident dict (e.g., direct test post)
    elif "id" in payload or "incident_key" in payload:
        incidents_raw.append(payload)

    if not incidents_raw:
        # Non-trigger event (acknowledge, resolve) — accept but no-op
        return {"status": "ok", "investigation_id": None, "note": "non-trigger event ignored"}

    results = []
    for raw in incidents_raw:
        try:
            incident = Incident.from_pagerduty(raw)
        except (ValueError, KeyError) as exc:
            logger.warning("PagerDuty webhook: skipping invalid incident: %s", exc)
            continue
        results.append(await _accept_and_dispatch(incident))

    if not results:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="No valid incidents in payload")
    return results[0] if len(results) == 1 else {"accepted": results}


# ---------------------------------------------------------------------------
# ServiceNow webhook
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/webhooks/servicenow",
    status_code=status.HTTP_202_ACCEPTED,
    summary="ServiceNow incident webhook",
)
async def servicenow_webhook(request: Request):
    """Accept ServiceNow incident webhook and trigger investigation.

    ServiceNow sends incident records with fields:
      number, short_description, cmdb_ci, priority (1-4), state, description.

    Only active/open incidents (state != resolved/closed) trigger investigations.

    Optional signature validation via SNOW_WEBHOOK_SECRET env var.
    """
    await _check_sig(request, _SNOW_SECRET, "X-ServiceNow-Hmac")
    payload = await request.json()

    # SNOW may wrap payload in "result" or send directly
    raw = payload.get("result", payload)
    if isinstance(raw, list):
        raw = raw[0] if raw else {}

    # Skip resolved/closed incidents
    state = str(raw.get("state", "1"))
    if state in ("6", "7"):  # 6=resolved, 7=closed
        return {"status": "ok", "investigation_id": None, "note": "resolved/closed incident ignored"}

    try:
        incident = Incident.from_servicenow(raw)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Invalid ServiceNow payload: {exc}")

    return await _accept_and_dispatch(incident)


# ---------------------------------------------------------------------------
# Manual trigger endpoint
# ---------------------------------------------------------------------------

class ManualIncidentRequest(BaseModel):
    """Manual incident submission — accepts any format, auto-detected."""

    incident_id: str
    summary: str = ""
    affected_service: str = "unknown"
    severity: int = 3
    description: str = ""
    source: str = "manual"
    tags: list[str] = []


@router.post(
    "/api/v1/incidents",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Manual incident submission",
)
async def submit_incident(req: ManualIncidentRequest, request: Request):
    """Submit any incident manually to trigger an investigation.

    Accepts a normalized incident payload directly — no transformation needed.
    Useful for:
    - Integration testing
    - One-off investigations triggered from CI/CD pipelines
    - Alerts from monitoring tools not natively supported

    No authentication required (protected by network/API gateway in production).
    """
    trace_id = getattr(request.state, "trace_id", "")
    try:
        incident = Incident(
            incident_id=req.incident_id,
            summary=req.summary or req.description,
            affected_service=req.affected_service,
            severity=req.severity,
            description=req.description,
            source=req.source,
            tags=req.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return await _accept_and_dispatch(incident, trace_id=trace_id)


# ---------------------------------------------------------------------------
# Health / catalog
# ---------------------------------------------------------------------------

@router.get("/api/v1/webhooks", tags=["intake"])
async def list_webhooks():
    """List available webhook endpoints and their configuration status."""
    return {
        "webhooks": [
            {
                "name": "moogsoft",
                "url": "/api/v1/webhooks/moogsoft",
                "method": "POST",
                "signature_validation": bool(_MOOGSOFT_SECRET),
                "env_var": "MOOGSOFT_WEBHOOK_SECRET",
            },
            {
                "name": "pagerduty",
                "url": "/api/v1/webhooks/pagerduty",
                "method": "POST",
                "signature_validation": bool(_PAGERDUTY_SECRET),
                "env_var": "PAGERDUTY_WEBHOOK_SECRET",
            },
            {
                "name": "servicenow",
                "url": "/api/v1/webhooks/servicenow",
                "method": "POST",
                "signature_validation": bool(_SNOW_SECRET),
                "env_var": "SNOW_WEBHOOK_SECRET",
            },
            {
                "name": "manual",
                "url": "/api/v1/incidents",
                "method": "POST",
                "signature_validation": False,
                "env_var": None,
            },
        ]
    }
