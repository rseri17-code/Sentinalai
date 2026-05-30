"""Webhook intake endpoints for external alerting systems.

Routes:
  POST /api/v1/webhooks/moogsoft    → Moogsoft alert webhook
  POST /api/v1/webhooks/pagerduty   → PagerDuty event webhook (v2/v3)
  POST /api/v1/webhooks/servicenow  → ServiceNow incident webhook
  POST /api/v1/webhooks/opsgenie    → OpsGenie alert webhook
  POST /api/v1/webhooks/grafana     → Grafana alerting webhook (unified + legacy)
  POST /api/v1/webhooks/cloudwatch  → AWS CloudWatch alarm webhook (SNS-wrapped)
  POST /api/v1/incidents            → Manual incident submission (any format)

All endpoints:
  1. Validate optional HMAC signature
  2. Deduplicate via AlertDeduplicator (fingerprint + cooldown window)
  3. Normalize to canonical Incident model
  4. Create IncidentState record in state store
  5. Dispatch investigation asynchronously
  6. Notify via NotificationRouter on completion
  7. Return 202 Accepted with investigation_id

Signature validation (opt-in via env vars):
  MOOGSOFT_WEBHOOK_SECRET   — shared secret for X-Moogsoft-Signature header
  PAGERDUTY_WEBHOOK_SECRET  — shared secret for X-PagerDuty-Signature header
  SNOW_WEBHOOK_SECRET       — shared secret for X-ServiceNow-Hmac header
  OPSGENIE_WEBHOOK_SECRET   — shared secret for X-OpsGenie-Hmac header
  GRAFANA_WEBHOOK_SECRET    — shared secret for X-Grafana-Signature header
  CLOUDWATCH_WEBHOOK_SECRET — shared secret for X-CloudWatch-Signature header
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
# Webhook auth mode
# ---------------------------------------------------------------------------

# When REQUIRE_WEBHOOK_AUTH=true, every webhook endpoint requires a valid HMAC
# signature.  Requests without a configured secret or with a missing/invalid
# header are rejected with 401.  In dev/test (default: false) unsigned webhooks
# are accepted to match the original opt-in behaviour.
REQUIRE_WEBHOOK_AUTH: bool = os.environ.get("REQUIRE_WEBHOOK_AUTH", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Webhook secrets
# ---------------------------------------------------------------------------

_MOOGSOFT_SECRET = os.environ.get("MOOGSOFT_WEBHOOK_SECRET", "")
_PAGERDUTY_SECRET = os.environ.get("PAGERDUTY_WEBHOOK_SECRET", "")
_SNOW_SECRET = os.environ.get("SNOW_WEBHOOK_SECRET", "")
_OPSGENIE_SECRET = os.environ.get("OPSGENIE_WEBHOOK_SECRET", "")
_GRAFANA_SECRET = os.environ.get("GRAFANA_WEBHOOK_SECRET", "")
_CLOUDWATCH_SECRET = os.environ.get("CLOUDWATCH_WEBHOOK_SECRET", "")

# Mapping of source name → secret (used for startup validation)
_WEBHOOK_SECRETS: dict[str, str] = {
    "moogsoft":   _MOOGSOFT_SECRET,
    "pagerduty":  _PAGERDUTY_SECRET,
    "servicenow": _SNOW_SECRET,
    "opsgenie":   _OPSGENIE_SECRET,
    "grafana":    _GRAFANA_SECRET,
    "cloudwatch": _CLOUDWATCH_SECRET,
}


def validate_webhook_secrets_at_startup() -> None:
    """Raise RuntimeError at startup if REQUIRE_WEBHOOK_AUTH=true but any secret is unset.

    Call this from the application lifespan / startup handler so the server
    refuses to start rather than silently accepting unauthenticated webhooks.
    """
    if not REQUIRE_WEBHOOK_AUTH:
        return
    missing = [name for name, secret in _WEBHOOK_SECRETS.items() if not secret]
    if missing:
        raise RuntimeError(
            f"REQUIRE_WEBHOOK_AUTH=true but webhook secrets are not configured for: "
            f"{', '.join(missing)}. "
            f"Set the corresponding *_WEBHOOK_SECRET environment variables or "
            f"disable REQUIRE_WEBHOOK_AUTH for non-production deployments."
        )


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
    """Validate HMAC signature on a webhook request.

    Behaviour:
    - REQUIRE_WEBHOOK_AUTH=true, secret configured: header required and verified.
    - REQUIRE_WEBHOOK_AUTH=true, secret NOT configured: 401 — misconfiguration.
    - REQUIRE_WEBHOOK_AUTH=false (default), secret configured: header required and verified.
    - REQUIRE_WEBHOOK_AUTH=false (default), secret NOT configured: skip (opt-in legacy mode).
    """
    if REQUIRE_WEBHOOK_AUTH and not secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Webhook auth required (REQUIRE_WEBHOOK_AUTH=true) but "
                f"no secret is configured for this source. "
                f"Set the corresponding *_WEBHOOK_SECRET environment variable."
            ),
        )
    if not secret:
        return  # opt-in legacy mode: no secret = skip validation
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

async def _accept_and_dispatch(
    incident: Incident, trace_id: str = "", org_id: str = ""
) -> dict[str, Any]:
    """Deduplicate, persist IncidentState, and kick off investigation."""
    # ── Alert deduplication ────────────────────────────────────────────────
    investigation_id = str(uuid.uuid4())
    try:
        from supervisor.alert_dedup import get_deduplicator
        dedup = get_deduplicator()
        dedup_result = dedup.check_and_register(
            incident_id=incident.incident_id,
            service=incident.affected_service,
            severity=incident.severity,
            tags=incident.tags,
            investigation_id=investigation_id,
        )
        if dedup_result.is_duplicate:
            logger.info(
                "Dedup HIT: incident=%s → existing=%s (%.0fs remaining)",
                incident.incident_id, dedup_result.existing_investigation_id,
                dedup_result.cooldown_remaining_secs,
            )
            return {
                "status": "deduplicated",
                "investigation_id": dedup_result.existing_investigation_id,
                "incident_id": incident.incident_id,
                "source": incident.source,
                "reason": dedup_result.reason,
                "ws_url": f"/ws/investigations/{dedup_result.existing_investigation_id}",
            }
    except Exception as exc:
        logger.debug("Dedup check failed (non-fatal): %s", exc)

    state = IncidentState(
        investigation_id=investigation_id,
        incident_id=incident.incident_id,
        trace_id=trace_id,
        status=InvestigationStatus.PENDING,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    store = get_state_store()
    await store.put_state(state)

    asyncio.create_task(_run_investigation(investigation_id, incident, trace_id, store, org_id))

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
    org_id: str = "",
) -> None:
    """Run investigation in a thread pool, update state when done, notify on completion."""
    state = await store.get_state(investigation_id)
    if state:
        state.status = InvestigationStatus.RUNNING
        await store.put_state(state)

    loop = asyncio.get_event_loop()
    result = None
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

        # ── Notify on success ────────────────────────────────────────────
        _notify_rca_complete(investigation_id, incident.incident_id, result, org_id)

    except Exception as exc:
        logger.error("Intake investigation %s failed: %s", investigation_id, exc)
        state = await store.get_state(investigation_id)
        if state:
            state.status = InvestigationStatus.FAILED
            await store.put_state(state)

        _notify_investigation_failed(investigation_id, incident.incident_id, exc, org_id)


def _notify_rca_complete(
    investigation_id: str, incident_id: str, result: Any, org_id: str
) -> None:
    try:
        from integrations.notification_router import get_notification_router
        get_notification_router().notify_rca_complete(
            investigation_id=investigation_id,
            incident_id=incident_id,
            rca_result=result or {},
        )
    except Exception as exc:
        logger.debug("Notification skipped: %s", exc)


def _notify_investigation_failed(
    investigation_id: str, incident_id: str, error: Exception, org_id: str
) -> None:
    try:
        from integrations.notification_router import get_notification_router
        get_notification_router().notify_investigation_failed(
            investigation_id=investigation_id,
            incident_id=incident_id,
            error=error,
        )
    except Exception as exc:
        logger.debug("Failure notification skipped: %s", exc)


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
# OpsGenie webhook
# ---------------------------------------------------------------------------

_OPSGENIE_ALERT_TRIGGERS = {"Create"}


@router.post(
    "/api/v1/webhooks/opsgenie",
    status_code=status.HTTP_202_ACCEPTED,
    summary="OpsGenie alert webhook",
)
async def opsgenie_webhook(request: Request):
    """Accept OpsGenie alert webhook and trigger investigation.

    OpsGenie sends webhook payloads with fields:
      action, alert.alertId, alert.message, alert.entity, alert.priority,
      alert.tags, alert.details

    Only 'Create' actions trigger investigations.

    Optional signature validation via OPSGENIE_WEBHOOK_SECRET env var.
    """
    await _check_sig(request, _OPSGENIE_SECRET, "X-OpsGenie-Hmac")
    payload = await request.json()

    action = payload.get("action", "")
    if action not in _OPSGENIE_ALERT_TRIGGERS:
        return {"status": "ok", "investigation_id": None, "note": f"action '{action}' ignored"}

    alert = payload.get("alert", payload)
    if not alert:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty OpsGenie payload")

    try:
        incident = _opsgenie_to_incident(alert)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Invalid OpsGenie payload: {exc}")

    return await _accept_and_dispatch(incident)


def _opsgenie_to_incident(alert: dict) -> "Incident":
    """Normalize an OpsGenie alert dict to a canonical Incident."""
    # OpsGenie priority: P1 (critical) → P5 (info); map to 1-5
    priority_map = {"P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}
    priority_str = str(alert.get("priority", "P3")).upper()
    severity = priority_map.get(priority_str, 3)

    tags_raw = alert.get("tags", [])
    tags = tags_raw if isinstance(tags_raw, list) else [str(tags_raw)]

    # Entity is the affected service in OpsGenie
    entity = alert.get("entity", alert.get("source", "unknown"))

    return Incident(
        incident_id=str(alert.get("alertId", alert.get("id", str(uuid.uuid4())))),
        summary=str(alert.get("message", alert.get("summary", ""))),
        affected_service=entity,
        severity=severity,
        source="opsgenie",
        status="open",
        description=str(alert.get("description", alert.get("details", ""))),
        tags=tags,
        raw_data=alert,
    )


# ---------------------------------------------------------------------------
# Grafana alerting webhook
# ---------------------------------------------------------------------------

_GRAFANA_FIRING_STATES = {"alerting", "firing"}


@router.post(
    "/api/v1/webhooks/grafana",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Grafana unified alerting webhook",
)
async def grafana_webhook(request: Request):
    """Accept Grafana unified alerting webhook and trigger investigation.

    Supports both:
    - Grafana 9+ unified alerting: {"alerts": [...], "status": "firing"}
    - Grafana legacy alerting:     {"state": "alerting", "ruleName": "..."}

    Only 'firing' / 'alerting' states trigger investigations.

    Optional signature validation via GRAFANA_WEBHOOK_SECRET env var.
    """
    await _check_sig(request, _GRAFANA_SECRET, "X-Grafana-Signature")
    payload = await request.json()

    results = []

    # Grafana unified alerting (9+): top-level "alerts" array
    if "alerts" in payload:
        for alert in payload.get("alerts", []):
            state = alert.get("status", "").lower()
            if state not in _GRAFANA_FIRING_STATES:
                continue
            try:
                incident = _grafana_unified_to_incident(alert, payload)
                results.append(await _accept_and_dispatch(incident))
            except Exception as exc:
                logger.warning("Grafana unified alert skipped: %s", exc)

    # Legacy Grafana alerting: flat dict with "state"
    elif payload.get("state", "").lower() in _GRAFANA_FIRING_STATES:
        try:
            incident = _grafana_legacy_to_incident(payload)
            results.append(await _accept_and_dispatch(incident))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Invalid Grafana payload: {exc}")
    else:
        state = payload.get("status", payload.get("state", "unknown"))
        return {"status": "ok", "investigation_id": None, "note": f"state '{state}' ignored"}

    if not results:
        return {"status": "ok", "investigation_id": None, "note": "no firing alerts"}

    return results[0] if len(results) == 1 else {"accepted": results}


def _grafana_unified_to_incident(alert: dict, envelope: dict) -> "Incident":
    """Normalize a Grafana unified alert to a canonical Incident."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    service = (labels.get("service") or labels.get("job") or
               labels.get("namespace") or envelope.get("groupLabels", {}).get("service", "unknown"))

    # Map Grafana severity label → 1-5
    sev_str = labels.get("severity", "warning").lower()
    severity_map = {"critical": 1, "high": 2, "warning": 3, "low": 4, "info": 5}
    severity = severity_map.get(sev_str, 3)

    summary = (annotations.get("summary") or annotations.get("description") or
               alert.get("alertname") or labels.get("alertname", ""))

    tags = [f"{k}={v}" for k, v in labels.items() if k not in ("__name__", "alertname")]

    return Incident(
        incident_id=alert.get("fingerprint", str(uuid.uuid4())),
        summary=summary,
        affected_service=service,
        severity=severity,
        source="grafana",
        status="open",
        created_at=alert.get("startsAt", ""),
        description=annotations.get("description", summary),
        tags=tags[:10],
        raw_data=alert,
    )


def _grafana_legacy_to_incident(payload: dict) -> "Incident":
    """Normalize a Grafana legacy alert payload to a canonical Incident."""
    rule_name = payload.get("ruleName", payload.get("title", ""))
    eval_matches = payload.get("evalMatches", [])
    service = "unknown"
    if eval_matches and isinstance(eval_matches[0], dict):
        tags_raw = eval_matches[0].get("tags", {})
        service = tags_raw.get("service", tags_raw.get("job", "unknown"))

    return Incident(
        incident_id=str(payload.get("ruleId", str(uuid.uuid4()))),
        summary=rule_name,
        affected_service=service,
        severity=3,  # legacy format doesn't expose severity
        source="grafana",
        status="open",
        description=payload.get("message", rule_name),
        tags=[],
        raw_data=payload,
    )


# ---------------------------------------------------------------------------
# AWS CloudWatch alarm webhook (SNS-wrapped)
# ---------------------------------------------------------------------------

_CW_ALARM_TRIGGERS = {"ALARM"}


@router.post(
    "/api/v1/webhooks/cloudwatch",
    status_code=status.HTTP_202_ACCEPTED,
    summary="AWS CloudWatch alarm webhook (SNS-wrapped)",
)
async def cloudwatch_webhook(request: Request):
    """Accept AWS CloudWatch alarm via SNS HTTP subscription.

    SNS delivers CloudWatch alarms as:
    {
      "Type": "Notification",
      "Message": "{...alarm JSON...}",
      "Subject": "ALARM: ...",
      "TopicArn": "arn:aws:sns:..."
    }

    Only alarms in 'ALARM' state trigger investigations.
    Subscription confirmations (Type=SubscriptionConfirmation) are acknowledged.

    Optional signature validation via CLOUDWATCH_WEBHOOK_SECRET env var.
    """
    await _check_sig(request, _CLOUDWATCH_SECRET, "X-CloudWatch-Signature")
    payload = await request.json()

    msg_type = payload.get("Type", "")

    # SNS subscription confirmation — return 200 OK so SNS marks it confirmed
    if msg_type == "SubscriptionConfirmation":
        subscribe_url = payload.get("SubscribeURL", "")
        logger.info("CloudWatch/SNS subscription confirmation received (SubscribeURL=%s)", subscribe_url[:80])
        return {"status": "ok", "note": "subscription confirmation received"}

    # SNS notification wrapping a CloudWatch alarm
    if msg_type == "Notification":
        import json as _json
        try:
            alarm = _json.loads(payload.get("Message", "{}"))
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="CloudWatch SNS message is not valid JSON")
    elif "AlarmName" in payload:
        # Direct alarm dict (non-SNS, e.g. from EventBridge or direct delivery)
        alarm = payload
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Unrecognized CloudWatch payload format")

    new_state = alarm.get("NewStateValue", "")
    if new_state not in _CW_ALARM_TRIGGERS:
        return {"status": "ok", "investigation_id": None, "note": f"alarm state '{new_state}' ignored"}

    try:
        incident = _cloudwatch_to_incident(alarm)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Invalid CloudWatch alarm: {exc}")

    return await _accept_and_dispatch(incident)


def _cloudwatch_to_incident(alarm: dict) -> "Incident":
    """Normalize a CloudWatch alarm dict to a canonical Incident."""
    alarm_name = alarm.get("AlarmName", "")
    description = alarm.get("AlarmDescription", alarm_name)
    namespace = alarm.get("Trigger", {}).get("Namespace", "AWS/Unknown")
    dimensions = alarm.get("Trigger", {}).get("Dimensions", [])

    # Extract service name from dimensions or namespace
    service = "unknown"
    for dim in dimensions:
        if isinstance(dim, dict):
            name = dim.get("name", dim.get("Name", ""))
            value = dim.get("value", dim.get("Value", ""))
            if name in ("FunctionName", "ServiceName", "ClusterName", "DBInstanceIdentifier", "LoadBalancer"):
                service = value
                break
    if service == "unknown":
        service = namespace.split("/")[-1].lower().replace(" ", "-")

    # Map namespace to rough severity
    critical_namespaces = {"AWS/RDS", "AWS/ElastiCache", "AWS/ELB", "AWS/ApplicationELB"}
    severity = 2 if namespace in critical_namespaces else 3

    tags = [f"namespace={namespace}", f"region={alarm.get('Region', 'unknown')}"]
    for dim in dimensions:
        if isinstance(dim, dict):
            n = dim.get("name", dim.get("Name", ""))
            v = dim.get("value", dim.get("Value", ""))
            if n and v:
                tags.append(f"{n}={v}")

    return Incident(
        incident_id=f"CW-{alarm_name[:40].replace(' ', '-')}",
        summary=f"CloudWatch ALARM: {alarm_name}",
        affected_service=service,
        severity=severity,
        source="cloudwatch",
        status="open",
        description=description,
        tags=tags[:8],
        raw_data=alarm,
    )


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
                "name": "opsgenie",
                "url": "/api/v1/webhooks/opsgenie",
                "method": "POST",
                "signature_validation": bool(_OPSGENIE_SECRET),
                "env_var": "OPSGENIE_WEBHOOK_SECRET",
            },
            {
                "name": "grafana",
                "url": "/api/v1/webhooks/grafana",
                "method": "POST",
                "signature_validation": bool(_GRAFANA_SECRET),
                "env_var": "GRAFANA_WEBHOOK_SECRET",
            },
            {
                "name": "cloudwatch",
                "url": "/api/v1/webhooks/cloudwatch",
                "method": "POST",
                "signature_validation": bool(_CLOUDWATCH_SECRET),
                "env_var": "CLOUDWATCH_WEBHOOK_SECRET",
                "note": "Expects SNS-wrapped CloudWatch alarm; also accepts direct alarm dicts",
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
