"""Tests for the webhook intake API (agui/api/intake.py).

Tests cover:
  - Moogsoft webhook: single incident, list, invalid payload, signature
  - PagerDuty webhook: v2 format, v3 format, non-trigger events
  - ServiceNow webhook: open incident, resolved incident (no-op)
  - Manual trigger: valid, missing incident_id
  - Webhook catalog endpoint
  - HMAC signature helpers
"""
from __future__ import annotations

import hashlib
import hmac
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# App fixture — mount the intake router with stubbed dependencies
# ---------------------------------------------------------------------------

@pytest.fixture()
def intake_client():
    """TestClient with intake router mounted, dependencies patched."""
    from agui.api.intake import router

    app = FastAPI()
    app.include_router(router)

    # Stub state store and dispatch so tests don't hit DB or agent
    _fake_store = AsyncMock()
    _fake_store.put_state = AsyncMock(return_value=None)

    def _consume_coro(coro):
        """close() the coroutine so Python doesn't warn about it never being awaited."""
        if hasattr(coro, "close"):
            coro.close()

    with (
        patch("agui.api.intake.get_state_store", return_value=_fake_store),
        patch("agui.api.intake.asyncio.create_task", side_effect=_consume_coro),
    ):
        with TestClient(app, raise_server_exceptions=True) as client:
            yield client


# ---------------------------------------------------------------------------
# Moogsoft webhook tests
# ---------------------------------------------------------------------------

MOOGSOFT_INCIDENT = {
    "incident_id": "INC-001",
    "summary": "API error rate spike",
    "service": "payment-service",
    "severity": 2,
    "status": "open",
}


class TestMoogsoftWebhook:
    def test_single_incident_returns_202(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=MOOGSOFT_INCIDENT)
        assert resp.status_code == 202

    def test_response_has_investigation_id(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=MOOGSOFT_INCIDENT)
        body = resp.json()
        assert "investigation_id" in body
        assert body["investigation_id"]  # non-empty

    def test_response_has_status_accepted(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=MOOGSOFT_INCIDENT)
        assert resp.json()["status"] == "accepted"

    def test_response_has_ws_url(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=MOOGSOFT_INCIDENT)
        assert resp.json()["ws_url"].startswith("/ws/investigations/")

    def test_incident_list_payload_accepted(self, intake_client):
        payload = {"incidents": [MOOGSOFT_INCIDENT, {**MOOGSOFT_INCIDENT, "incident_id": "INC-002"}]}
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=payload)
        assert resp.status_code == 202

    def test_empty_payload_returns_400(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json={})
        # Empty dict has no incident_id — should 400 on normalization
        assert resp.status_code in (400, 422)

    def test_source_is_moogsoft(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=MOOGSOFT_INCIDENT)
        assert resp.json()["source"] == "moogsoft"

    def test_incident_id_echoed(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=MOOGSOFT_INCIDENT)
        assert resp.json()["incident_id"] == "INC-001"

    def test_string_severity_normalized(self, intake_client):
        payload = {**MOOGSOFT_INCIDENT, "severity": "critical"}
        resp = intake_client.post("/api/v1/webhooks/moogsoft", json=payload)
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# PagerDuty webhook tests
# ---------------------------------------------------------------------------

PD_V2_PAYLOAD = {
    "messages": [
        {
            "event": "incident.trigger",
            "incident": {
                "id": "PD-12345",
                "title": "Service degraded",
                "urgency": "high",
                "service": {"summary": "checkout-service"},
                "status": "triggered",
            },
        }
    ]
}

PD_V3_PAYLOAD = {
    "event": {
        "event_type": "incident.triggered",
        "data": {
            "id": "PD-99999",
            "title": "DB connection failures",
            "urgency": "high",
            "service": {"summary": "database-service"},
            "status": "triggered",
        },
    }
}


class TestPagerDutyWebhook:
    def test_v2_trigger_returns_202(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=PD_V2_PAYLOAD)
        assert resp.status_code == 202

    def test_v3_trigger_returns_202(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=PD_V3_PAYLOAD)
        assert resp.status_code == 202

    def test_v2_response_has_investigation_id(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=PD_V2_PAYLOAD)
        assert "investigation_id" in resp.json()

    def test_non_trigger_event_is_noop(self, intake_client):
        payload = {
            "messages": [{"event": "incident.resolve", "incident": {"id": "PD-1"}}]
        }
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["investigation_id"] is None
        assert "ignored" in body.get("note", "").lower()

    def test_v3_non_trigger_is_noop(self, intake_client):
        payload = {"event": {"event_type": "incident.acknowledged", "data": {"id": "PD-2"}}}
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=payload)
        assert resp.json()["investigation_id"] is None

    def test_source_is_pagerduty(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=PD_V2_PAYLOAD)
        assert resp.json()["source"] == "pagerduty"

    def test_direct_incident_dict_accepted(self, intake_client):
        payload = {"id": "PD-DIRECT", "title": "DB error", "urgency": "high",
                   "service": {"summary": "api"}, "status": "triggered"}
        resp = intake_client.post("/api/v1/webhooks/pagerduty", json=payload)
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# ServiceNow webhook tests
# ---------------------------------------------------------------------------

SNOW_INCIDENT = {
    "number": "INC0099001",
    "short_description": "Payment service timeout",
    "cmdb_ci": "payment-service",
    "priority": 2,
    "state": "1",
    "sys_created_on": "2024-01-15T14:00:00Z",
}


class TestServiceNowWebhook:
    def test_open_incident_returns_202(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/servicenow", json=SNOW_INCIDENT)
        assert resp.status_code == 202

    def test_response_has_investigation_id(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/servicenow", json=SNOW_INCIDENT)
        assert "investigation_id" in resp.json()

    def test_resolved_incident_is_noop(self, intake_client):
        payload = {**SNOW_INCIDENT, "state": "6"}  # 6 = resolved
        resp = intake_client.post("/api/v1/webhooks/servicenow", json=payload)
        assert resp.status_code == 202
        body = resp.json()
        assert body["investigation_id"] is None
        assert "ignored" in body.get("note", "").lower()

    def test_closed_incident_is_noop(self, intake_client):
        payload = {**SNOW_INCIDENT, "state": "7"}  # 7 = closed
        resp = intake_client.post("/api/v1/webhooks/servicenow", json=payload)
        assert resp.json()["investigation_id"] is None

    def test_result_wrapped_payload(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/servicenow", json={"result": SNOW_INCIDENT})
        assert resp.status_code == 202

    def test_source_is_servicenow(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/servicenow", json=SNOW_INCIDENT)
        assert resp.json()["source"] == "servicenow"

    def test_incident_id_matches_snow_number(self, intake_client):
        resp = intake_client.post("/api/v1/webhooks/servicenow", json=SNOW_INCIDENT)
        assert resp.json()["incident_id"] == "INC0099001"


# ---------------------------------------------------------------------------
# Manual trigger endpoint
# ---------------------------------------------------------------------------

MANUAL_INCIDENT = {
    "incident_id": "MANUAL-001",
    "summary": "High memory usage on API nodes",
    "affected_service": "api-service",
    "severity": 2,
    "description": "Memory usage above 90% on 3 of 5 nodes",
    "source": "manual",
    "tags": ["memory", "api"],
}


class TestManualTrigger:
    def test_valid_incident_returns_202(self, intake_client):
        resp = intake_client.post("/api/v1/incidents", json=MANUAL_INCIDENT)
        assert resp.status_code == 202

    def test_response_has_investigation_id(self, intake_client):
        resp = intake_client.post("/api/v1/incidents", json=MANUAL_INCIDENT)
        assert "investigation_id" in resp.json()

    def test_source_preserved(self, intake_client):
        resp = intake_client.post("/api/v1/incidents", json=MANUAL_INCIDENT)
        assert resp.json()["source"] == "manual"

    def test_incident_id_echoed(self, intake_client):
        resp = intake_client.post("/api/v1/incidents", json=MANUAL_INCIDENT)
        assert resp.json()["incident_id"] == "MANUAL-001"

    def test_missing_incident_id_returns_422(self, intake_client):
        payload = {k: v for k, v in MANUAL_INCIDENT.items() if k != "incident_id"}
        resp = intake_client.post("/api/v1/incidents", json=payload)
        assert resp.status_code == 422

    def test_minimal_payload_accepted(self, intake_client):
        resp = intake_client.post("/api/v1/incidents",
                                  json={"incident_id": "MIN-001", "summary": "Test"})
        assert resp.status_code == 202

    def test_ws_url_in_response(self, intake_client):
        resp = intake_client.post("/api/v1/incidents", json=MANUAL_INCIDENT)
        assert resp.json()["ws_url"].startswith("/ws/investigations/")


# ---------------------------------------------------------------------------
# Webhook catalog
# ---------------------------------------------------------------------------

class TestWebhookCatalog:
    def test_returns_four_webhooks(self, intake_client):
        resp = intake_client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        assert len(resp.json()["webhooks"]) == 4

    def test_contains_moogsoft(self, intake_client):
        resp = intake_client.get("/api/v1/webhooks")
        names = [w["name"] for w in resp.json()["webhooks"]]
        assert "moogsoft" in names

    def test_contains_pagerduty(self, intake_client):
        resp = intake_client.get("/api/v1/webhooks")
        names = [w["name"] for w in resp.json()["webhooks"]]
        assert "pagerduty" in names

    def test_contains_servicenow(self, intake_client):
        resp = intake_client.get("/api/v1/webhooks")
        names = [w["name"] for w in resp.json()["webhooks"]]
        assert "servicenow" in names

    def test_contains_manual(self, intake_client):
        resp = intake_client.get("/api/v1/webhooks")
        names = [w["name"] for w in resp.json()["webhooks"]]
        assert "manual" in names

    def test_each_webhook_has_url(self, intake_client):
        resp = intake_client.get("/api/v1/webhooks")
        for wh in resp.json()["webhooks"]:
            assert "url" in wh


# ---------------------------------------------------------------------------
# HMAC signature helper unit tests
# ---------------------------------------------------------------------------

class TestHmacHelper:
    def test_valid_signature_passes(self):
        from agui.api.intake import _verify_hmac_sha256
        secret = "mysecret"
        body = b'{"test": "data"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_hmac_sha256(secret, body, sig) is True

    def test_sha256_prefixed_signature_passes(self):
        from agui.api.intake import _verify_hmac_sha256
        secret = "mysecret"
        body = b'{"test": "data"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_hmac_sha256(secret, body, sig) is True

    def test_wrong_signature_fails(self):
        from agui.api.intake import _verify_hmac_sha256
        assert _verify_hmac_sha256("secret", b"body", "wrong") is False

    def test_empty_signature_fails(self):
        from agui.api.intake import _verify_hmac_sha256
        assert _verify_hmac_sha256("secret", b"body", "") is False
