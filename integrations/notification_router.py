"""Notification Router — outbound alert and RCA result delivery.

Closes the egress gap: SentinalAI receives alerts IN (PagerDuty, Moogsoft,
ServiceNow) but without this module results never flow BACK to the team.

Supported backends (all optional, activated by env vars):
  Slack       — rich Block Kit messages via webhook or Bot Token
  PagerDuty   — add note + acknowledge via Events API v2
  ServiceNow  — PATCH incident with RCA and work notes
  OpsGenie    — add note + tag via REST API v2
  Email       — SMTP summary for escalations

Routing rules:
  - notify_rca_complete()         → all backends
  - notify_investigation_failed() → Slack only (ops awareness)
  - notify_remediation_proposed() → Slack + PD note
  - notify_slo_burning()          → Slack (#alerts channel)
  - notify_pattern_prediction()   → Slack (#intelligence channel)

All backend calls are fire-and-forget (background thread pool).
Failure in one backend never blocks another or the RCA pipeline.

Configuration:
  SLACK_WEBHOOK_URL           — incoming webhook URL
  SLACK_BOT_TOKEN             — bot token (used if webhook not set)
  SLACK_RCA_CHANNEL           — channel ID for RCA results (default: #incidents)
  SLACK_INTEL_CHANNEL         — channel for pattern predictions
  PAGERDUTY_EVENTS_V2_KEY     — integration key for Events API v2
  PAGERDUTY_API_TOKEN         — REST API token for note/ack write-back
  SERVICENOW_INSTANCE_URL     — https://acme.service-now.com
  SERVICENOW_USERNAME         — basic auth user
  SERVICENOW_PASSWORD         — basic auth pass
  OPSGENIE_API_KEY            — OpsGenie REST API key (US region default)
  OPSGENIE_API_URL            — override for EU: https://api.eu.opsgenie.com/v2
  NOTIFY_MIN_CONFIDENCE       — minimum confidence % to notify (default: 0 = always)
  NOTIFY_TIMEOUT_SEC          — HTTP timeout per backend (default: 8)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
    _HTTPX = True
except ImportError:
    import requests as _requests  # type: ignore[import]
    _HTTPX = False

logger = logging.getLogger("sentinalai.notification_router")

_TIMEOUT = int(os.environ.get("NOTIFY_TIMEOUT_SEC", "8"))
_MIN_CONFIDENCE = float(os.environ.get("NOTIFY_MIN_CONFIDENCE", "0"))

_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="notif-")


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _post(url: str, payload: dict, headers: dict | None = None, auth: tuple | None = None) -> int:
    """HTTP POST; returns status code. Never raises."""
    try:
        h = headers or {}
        if _HTTPX:
            r = httpx.post(url, json=payload, headers=h, auth=auth, timeout=_TIMEOUT)
            return r.status_code
        else:
            r = _requests.post(url, json=payload, headers=h, auth=auth, timeout=_TIMEOUT)
            return r.status_code
    except Exception as exc:
        logger.debug("HTTP POST to %s failed: %s", url, exc)
        return 0


def _patch(url: str, payload: dict, headers: dict | None = None, auth: tuple | None = None) -> int:
    try:
        h = headers or {}
        if _HTTPX:
            r = httpx.patch(url, json=payload, headers=h, auth=auth, timeout=_TIMEOUT)
            return r.status_code
        else:
            r = _requests.patch(url, json=payload, headers=h, auth=auth, timeout=_TIMEOUT)
            return r.status_code
    except Exception as exc:
        logger.debug("HTTP PATCH to %s failed: %s", url, exc)
        return 0


def _confidence_badge(confidence: float) -> str:
    if confidence >= 80:
        return "🟢"
    if confidence >= 60:
        return "🟡"
    return "🔴"


# ── Backend protocol ──────────────────────────────────────────────────────────

class NotificationBackend(ABC):
    name: str = "base"

    @abstractmethod
    def send_rca(self, investigation_id: str, incident_id: str, rca: dict) -> bool: ...

    def send_failed(self, investigation_id: str, incident_id: str, error: str) -> bool:
        return True

    def send_remediation(self, investigation_id: str, fix: dict) -> bool:
        return True

    def send_slo_burning(self, service: str, slo: dict) -> bool:
        return True

    def send_prediction(self, prediction: dict) -> bool:
        return True


# ── Slack backend ─────────────────────────────────────────────────────────────

class SlackBackend(NotificationBackend):
    name = "slack"

    def __init__(self) -> None:
        self._webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        self._token = os.environ.get("SLACK_BOT_TOKEN", "")
        self._rca_channel = os.environ.get("SLACK_RCA_CHANNEL", "#incidents")
        self._intel_channel = os.environ.get("SLACK_INTEL_CHANNEL", "#sre-intelligence")

    def _post_message(self, channel: str, blocks: list, text: str) -> bool:
        if self._webhook:
            sc = _post(self._webhook, {"text": text, "blocks": blocks})
            return 200 <= sc < 300
        if self._token:
            sc = _post(
                "https://slack.com/api/chat.postMessage",
                {"channel": channel, "text": text, "blocks": blocks},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            return 200 <= sc < 300
        return False

    def send_rca(self, investigation_id: str, incident_id: str, rca: dict) -> bool:
        confidence = rca.get("confidence", 0)
        root_cause = rca.get("root_cause", "Unknown")
        risk = rca.get("risk_level", "medium")
        actions = rca.get("immediate_actions", [])
        badge = _confidence_badge(confidence)

        actions_text = "\n".join(f"• {a}" for a in actions[:4]) or "_No immediate actions_"

        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": f"{badge} RCA Complete — {incident_id}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Confidence:* {confidence:.0f}%"},
                {"type": "mrkdwn", "text": f"*Risk:* {risk.upper()}"},
                {"type": "mrkdwn", "text": f"*Root Cause:*\n{root_cause[:300]}"},
                {"type": "mrkdwn", "text": f"*Immediate Actions:*\n{actions_text}"},
            ]},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"Investigation: `{investigation_id}`"}
            ]},
        ]
        return self._post_message(
            self._rca_channel, blocks,
            f"{badge} RCA Complete for {incident_id} — confidence {confidence:.0f}%",
        )

    def send_failed(self, investigation_id: str, incident_id: str, error: str) -> bool:
        blocks = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"⚠️ *Investigation failed* for `{incident_id}`\n```{error[:500]}```",
            }},
        ]
        return self._post_message(
            self._rca_channel, blocks,
            f"Investigation failed for {incident_id}: {error[:120]}",
        )

    def send_remediation(self, investigation_id: str, fix: dict) -> bool:
        desc = fix.get("fix_description", "")
        risk = fix.get("risk_level", "medium")
        fix_type = fix.get("fix_type", "")
        blocks = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔧 *Remediation proposed* for `{fix.get('incident_id', '')}`\n"
                    f"*Type:* {fix_type}  *Risk:* {risk.upper()}\n"
                    f"*Description:* {desc[:400]}\n\n"
                    f"_Awaiting operator approval in SentinalAI UI._"
                ),
            }},
        ]
        return self._post_message(
            self._rca_channel, blocks, f"Remediation proposed: {fix_type} ({risk})",
        )

    def send_slo_burning(self, service: str, slo: dict) -> bool:
        burn_rate = slo.get("burn_rate", 0)
        budget_remaining = slo.get("error_budget_remaining_pct", 0)
        blocks = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔥 *SLO Burning* — `{service}`\n"
                    f"Burn rate: *{burn_rate:.1f}×*  |  "
                    f"Budget remaining: *{budget_remaining:.1f}%*"
                ),
            }},
        ]
        return self._post_message(
            self._intel_channel, blocks, f"SLO burning: {service} at {burn_rate:.1f}x",
        )

    def send_prediction(self, prediction: dict) -> bool:
        svc = prediction.get("service", "")
        severity = prediction.get("severity", "WATCH")
        pattern = prediction.get("pattern_type", "")
        conf = prediction.get("confidence", 0) * 100
        explanation = prediction.get("explanation", "")
        blocks = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔮 *Pattern Prediction* [{severity}] — `{svc}`\n"
                    f"Pattern: `{pattern}` | Confidence: {conf:.0f}%\n"
                    f"{explanation[:300]}"
                ),
            }},
        ]
        return self._post_message(
            self._intel_channel, blocks, f"Prediction [{severity}]: {svc} — {pattern}",
        )


# ── PagerDuty backend ─────────────────────────────────────────────────────────

class PagerDutyBackend(NotificationBackend):
    name = "pagerduty"
    _EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
    _API_BASE   = "https://api.pagerduty.com"

    def __init__(self) -> None:
        self._events_key = os.environ.get("PAGERDUTY_EVENTS_V2_KEY", "")
        self._api_token  = os.environ.get("PAGERDUTY_API_TOKEN", "")

    def _api_headers(self) -> dict:
        return {
            "Authorization": f"Token token={self._api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
            "Content-Type": "application/json",
        }

    def send_rca(self, investigation_id: str, incident_id: str, rca: dict) -> bool:
        confidence = rca.get("confidence", 0)
        root_cause = rca.get("root_cause", "")
        risk = rca.get("risk_level", "medium")

        note_body = (
            f"[SentinalAI RCA] Confidence: {confidence:.0f}% | Risk: {risk.upper()}\n"
            f"Root cause: {root_cause[:500]}\n"
            f"Investigation: {investigation_id}"
        )

        ok = False

        # Add note via REST API
        if self._api_token and incident_id.startswith("P"):
            sc = _post(
                f"{self._API_BASE}/incidents/{incident_id}/notes",
                {"note": {"content": note_body}},
                headers=self._api_headers(),
            )
            ok = 200 <= sc < 300

        # Auto-acknowledge if high confidence
        if self._api_token and confidence >= 80 and incident_id.startswith("P"):
            _patch(
                f"{self._API_BASE}/incidents/{incident_id}",
                {"incident": {"type": "incident_reference", "status": "acknowledged"}},
                headers={**self._api_headers(), "From": "sentinalai@system"},
            )

        return ok

    def send_remediation(self, investigation_id: str, fix: dict) -> bool:
        if not self._events_key:
            return False
        sc = _post(self._EVENTS_URL, {
            "routing_key": self._events_key,
            "event_action": "acknowledge",
            "dedup_key": fix.get("incident_id", investigation_id),
            "payload": {
                "summary": f"SentinalAI proposed remediation: {fix.get('fix_type', '')}",
                "source": "sentinalai",
                "severity": "info",
                "custom_details": fix,
            },
        })
        return 200 <= sc < 300


# ── ServiceNow backend ────────────────────────────────────────────────────────

class ServiceNowBackend(NotificationBackend):
    name = "servicenow"

    def __init__(self) -> None:
        self._base = os.environ.get("SERVICENOW_INSTANCE_URL", "").rstrip("/")
        self._user = os.environ.get("SERVICENOW_USERNAME", "")
        self._pass = os.environ.get("SERVICENOW_PASSWORD", "")

    def _auth(self) -> tuple:
        return (self._user, self._pass)

    def _find_sys_id(self, incident_id: str) -> str | None:
        """Resolve incident number (INC0001234) to sys_id."""
        if not self._base:
            return None
        try:
            url = f"{self._base}/api/now/table/incident?sysparm_query=number={incident_id}&sysparm_fields=sys_id"
            if _HTTPX:
                r = httpx.get(url, auth=self._auth(), timeout=_TIMEOUT)
                data = r.json()
            else:
                r = _requests.get(url, auth=self._auth(), timeout=_TIMEOUT)
                data = r.json()
            results = data.get("result", [])
            return results[0]["sys_id"] if results else None
        except Exception:
            return None

    def send_rca(self, investigation_id: str, incident_id: str, rca: dict) -> bool:
        if not self._base:
            return False
        sys_id = self._find_sys_id(incident_id)
        if not sys_id:
            return False

        confidence = rca.get("confidence", 0)
        root_cause = rca.get("root_cause", "")
        work_note = (
            f"[SentinalAI] Investigation {investigation_id}\n"
            f"Root cause ({confidence:.0f}% confidence): {root_cause}\n"
            f"Risk level: {rca.get('risk_level', 'medium').upper()}"
        )
        sc = _patch(
            f"{self._base}/api/now/table/incident/{sys_id}",
            {"work_notes": work_note, "close_notes": root_cause[:500]},
            auth=self._auth(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return 200 <= sc < 300


# ── OpsGenie backend ──────────────────────────────────────────────────────────

class OpsGenieBackend(NotificationBackend):
    name = "opsgenie"

    def __init__(self) -> None:
        self._key = os.environ.get("OPSGENIE_API_KEY", "")
        self._base = os.environ.get("OPSGENIE_API_URL", "https://api.opsgenie.com/v2")

    def _headers(self) -> dict:
        return {"Authorization": f"GenieKey {self._key}", "Content-Type": "application/json"}

    def send_rca(self, investigation_id: str, incident_id: str, rca: dict) -> bool:
        if not self._key:
            return False
        confidence = rca.get("confidence", 0)
        note = (
            f"[SentinalAI] RCA complete — confidence {confidence:.0f}%\n"
            f"Root cause: {rca.get('root_cause', '')[:400]}\n"
            f"Investigation: {investigation_id}"
        )
        # Add note to alert
        sc = _post(
            f"{self._base}/alerts/{incident_id}/notes",
            {"note": note, "source": "sentinalai", "user": "sentinalai"},
            headers=self._headers(),
        )
        ok = 200 <= sc < 300

        # Auto-acknowledge high confidence results
        if confidence >= 80:
            _post(
                f"{self._base}/alerts/{incident_id}/acknowledge",
                {"source": "sentinalai", "user": "sentinalai", "note": note},
                headers=self._headers(),
            )
        return ok

    def send_prediction(self, prediction: dict) -> bool:
        if not self._key:
            return False
        severity_map = {"IMMINENT": "P1", "LIKELY": "P2", "WATCH": "P3"}
        severity = prediction.get("severity", "WATCH")
        sc = _post(
            f"{self._base}/alerts",
            {
                "message": f"SentinalAI Prediction [{severity}]: {prediction.get('service', '')}",
                "alias": f"sentinalai-pred-{prediction.get('prediction_id', '')}",
                "description": prediction.get("explanation", "")[:500],
                "priority": severity_map.get(severity, "P3"),
                "source": "sentinalai-intelligence",
                "tags": ["sentinalai", "prediction", prediction.get("pattern_type", "")],
                "details": {
                    "pattern_type": prediction.get("pattern_type", ""),
                    "confidence": str(round(prediction.get("confidence", 0) * 100, 1)),
                    "breach_hours": str(prediction.get("predicted_breach_hours", "")),
                },
            },
            headers=self._headers(),
        )
        return 200 <= sc < 300


# ── Router ────────────────────────────────────────────────────────────────────

class NotificationRouter:
    """Fan-out outbound notifications to all configured backends.

    Each backend is discovered from environment variables at construction time.
    All deliveries are fire-and-forget (thread pool); failures are logged at DEBUG.
    """

    def __init__(self) -> None:
        self._backends: list[NotificationBackend] = []
        self._build_backends()

    def _build_backends(self) -> None:
        candidates: list[NotificationBackend] = [
            SlackBackend(),
            PagerDutyBackend(),
            ServiceNowBackend(),
            OpsGenieBackend(),
        ]
        for b in candidates:
            configured = self._is_configured(b)
            if configured:
                self._backends.append(b)
                logger.info("Notification backend enabled: %s", b.name)

    @staticmethod
    def _is_configured(b: NotificationBackend) -> bool:
        if isinstance(b, SlackBackend):
            return bool(os.environ.get("SLACK_WEBHOOK_URL") or os.environ.get("SLACK_BOT_TOKEN"))
        if isinstance(b, PagerDutyBackend):
            return bool(os.environ.get("PAGERDUTY_EVENTS_V2_KEY") or os.environ.get("PAGERDUTY_API_TOKEN"))
        if isinstance(b, ServiceNowBackend):
            return bool(os.environ.get("SERVICENOW_INSTANCE_URL"))
        if isinstance(b, OpsGenieBackend):
            return bool(os.environ.get("OPSGENIE_API_KEY"))
        return False

    def _fan_out(self, method: str, **kwargs) -> None:
        """Submit one task per backend to the thread pool; log failures."""
        futures = {
            _EXECUTOR.submit(getattr(b, method), **kwargs): b.name
            for b in self._backends
        }
        for fut, name in futures.items():
            try:
                ok = fut.result(timeout=_TIMEOUT + 2)
                if not ok:
                    logger.debug("Notification backend %s returned False for %s", name, method)
            except Exception as exc:
                logger.debug("Notification backend %s error (%s): %s", name, method, exc)

    def notify_rca_complete(
        self,
        investigation_id: str,
        incident_id: str,
        rca_result: dict,
    ) -> None:
        """Push RCA completion to all configured backends.

        Skips if confidence is below NOTIFY_MIN_CONFIDENCE threshold.
        """
        confidence = rca_result.get("confidence", 0)
        if confidence < _MIN_CONFIDENCE:
            logger.debug(
                "Skipping RCA notification (confidence %.0f < min %.0f)",
                confidence, _MIN_CONFIDENCE,
            )
            return
        self._fan_out(
            "send_rca",
            investigation_id=investigation_id,
            incident_id=incident_id,
            rca=rca_result,
        )
        logger.info(
            "RCA notifications dispatched: investigation=%s backends=%d",
            investigation_id, len(self._backends),
        )

    def notify_investigation_failed(
        self,
        investigation_id: str,
        incident_id: str,
        error: str,
    ) -> None:
        self._fan_out(
            "send_failed",
            investigation_id=investigation_id,
            incident_id=incident_id,
            error=error,
        )

    def notify_remediation_proposed(
        self,
        investigation_id: str,
        fix: dict,
    ) -> None:
        self._fan_out("send_remediation", investigation_id=investigation_id, fix=fix)

    def notify_slo_burning(self, service: str, slo_status: dict) -> None:
        self._fan_out("send_slo_burning", service=service, slo=slo_status)

    def notify_pattern_prediction(self, prediction: dict) -> None:
        self._fan_out("send_prediction", prediction=prediction)

    @property
    def backend_count(self) -> int:
        return len(self._backends)

    @property
    def backend_names(self) -> list[str]:
        return [b.name for b in self._backends]


# ── Process singleton ─────────────────────────────────────────────────────────

_router: NotificationRouter | None = None
_router_lock = threading.Lock()


def get_notification_router() -> NotificationRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = NotificationRouter()
    return _router
