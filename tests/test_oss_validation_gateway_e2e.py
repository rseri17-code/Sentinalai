"""Optional live e2e against a running OSS validation stack.

Skipped in CI. To run after `docker compose -f deploy/oss-validation/docker-compose.yaml up`:

    OSS_VALIDATION_E2E=1 pytest tests/test_oss_validation_gateway_e2e.py -q
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OSS_VALIDATION_E2E", "").strip() not in {"1", "true", "yes"},
    reason="optional live e2e; set OSS_VALIDATION_E2E=1 against a running compose stack",
)

GATEWAY = os.environ.get("OSS_E2E_GATEWAY_URL", "http://127.0.0.1:9080").rstrip("/")


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{GATEWAY}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_health_live():
    req = urllib.request.Request(f"{GATEWAY}/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body["status"] == "ok"


def test_incident_logs_metrics_live():
    incident = _post("/invoke", {
        "toolName": "MoogsoftTarget___get_incident_by_id",
        "toolInput": {"incident_id": "INC-OSS-001"},
    })
    assert incident.get("incident"), incident
    logs = _post("/invoke", {
        "toolName": "SplunkTarget___search_oneshot",
        "toolInput": {"query": "timeout payment-service", "service": "payment-service"},
    })
    assert logs.get("logs", {}).get("count", 0) >= 1, logs
    metrics = _post("/invoke", {
        "toolName": "SysdigTarget___query_metrics",
        "toolInput": {"service": "payment-service", "metric": "response_time_ms"},
    })
    assert "metrics" in metrics, metrics
