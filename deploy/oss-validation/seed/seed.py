#!/usr/bin/env python3
"""Seed Prometheus metrics, Loki logs, and an Alertmanager firing alert.

stdlib only. Used by deploy/oss-validation/docker-compose.yaml.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("DEMO_SERVICE", "payment-service")
INCIDENT_ID = os.environ.get("DEMO_INCIDENT_ID", "INC-OSS-001")
DOWNSTREAM = os.environ.get("DEMO_DOWNSTREAM", "payment-db")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093").rstrip("/")
METRICS_PORT = int(os.environ.get("SEED_METRICS_PORT", "9100"))

# Values chosen so timeout analysis can see p95 >> baseline * 10.
LATENCY_P95 = float(os.environ.get("DEMO_LATENCY_P95_MS", "2500"))
LATENCY_BASELINE = float(os.environ.get("DEMO_LATENCY_BASELINE_MS", "80"))
LATENCY_P50 = float(os.environ.get("DEMO_LATENCY_P50_MS", "142"))
LATENCY_P99 = float(os.environ.get("DEMO_LATENCY_P99_MS", "3200"))
ERROR_RATE = float(os.environ.get("DEMO_ERROR_RATE", "0.12"))
REQUEST_RATE = float(os.environ.get("DEMO_REQUEST_RATE", "847"))
SATURATION = float(os.environ.get("DEMO_SATURATION_PCT", "94.2"))
MEMORY_BYTES = float(os.environ.get("DEMO_MEMORY_BYTES", "1200000000"))

_ready = {"loki": False, "alertmanager": False}


def _metrics_body() -> str:
    labels = f'service="{SERVICE}"'
    lines = [
        "# HELP demo_latency_p95_ms Demo p95 latency in milliseconds",
        "# TYPE demo_latency_p95_ms gauge",
        f"demo_latency_p95_ms{{{labels}}} {LATENCY_P95}",
        "# HELP demo_latency_baseline_p95_ms Demo baseline p95 latency",
        "# TYPE demo_latency_baseline_p95_ms gauge",
        f"demo_latency_baseline_p95_ms{{{labels}}} {LATENCY_BASELINE}",
        "# HELP demo_latency_p50_ms Demo p50 latency",
        "# TYPE demo_latency_p50_ms gauge",
        f"demo_latency_p50_ms{{{labels}}} {LATENCY_P50}",
        "# HELP demo_latency_p99_ms Demo p99 latency",
        "# TYPE demo_latency_p99_ms gauge",
        f"demo_latency_p99_ms{{{labels}}} {LATENCY_P99}",
        "# HELP demo_error_rate Demo error rate (0-1)",
        "# TYPE demo_error_rate gauge",
        f"demo_error_rate{{{labels}}} {ERROR_RATE}",
        "# HELP demo_request_rate Demo requests per second",
        "# TYPE demo_request_rate gauge",
        f"demo_request_rate{{{labels}}} {REQUEST_RATE}",
        "# HELP demo_saturation_pct Demo saturation percent",
        "# TYPE demo_saturation_pct gauge",
        f"demo_saturation_pct{{{labels}}} {SATURATION}",
        "# HELP process_resident_memory_bytes Demo resident memory",
        "# TYPE process_resident_memory_bytes gauge",
        f"process_resident_memory_bytes{{{labels}}} {MEMORY_BYTES}",
        "",
    ]
    return "\n".join(lines)


class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/metrics", "/"):
            body = _metrics_body().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/health", "/healthz"):
            status = 200 if _ready["loki"] and _ready["alertmanager"] else 503
            payload = json.dumps({"status": "ok" if status == 200 else "warming", "ready": _ready}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()


def _post_json(url: str, payload: object) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def _push_logs() -> None:
    line = f"ERROR timeout waiting for connection: {DOWNSTREAM}"
    payload = {
        "streams": [
            {
                "stream": {"service": SERVICE, "job": "oss-demo", "level": "error"},
                "values": [[str(time.time_ns()), line]],
            },
            {
                "stream": {"service": SERVICE, "job": "oss-demo", "level": "error"},
                "values": [[str(time.time_ns()), f"ERROR pool.exhausted service={SERVICE} waiting=47"]],
            },
        ]
    }
    _post_json(f"{LOKI_URL}/loki/api/v1/push", payload)


def _push_alert() -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = [
        {
            "labels": {
                "alertname": "PaymentServiceTimeout",
                "service": SERVICE,
                "incident_id": INCIDENT_ID,
                "severity": "critical",
            },
            "annotations": {
                "summary": f"{SERVICE} request timeout — upstream {DOWNSTREAM} not responding",
                "description": (
                    f"p95 latency {int(LATENCY_P95)}ms vs baseline {int(LATENCY_BASELINE)}ms. "
                    f"Timeouts waiting for connection: {DOWNSTREAM}"
                ),
                "incident_id": INCIDENT_ID,
            },
            "startsAt": now,
        }
    ]
    _post_json(f"{ALERTMANAGER_URL}/api/v2/alerts", payload)


def _retry_loop(name: str, fn: object, interval: float) -> None:
    while True:
        try:
            fn()
            _ready[name] = True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            _ready[name] = False
            print(f"seed {name} push failed: {exc}", flush=True)
        time.sleep(interval)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"seed metrics on :{METRICS_PORT} incident={INCIDENT_ID} service={SERVICE}", flush=True)
    threading.Thread(target=_retry_loop, args=("loki", _push_logs, 10.0), daemon=True).start()
    threading.Thread(target=_retry_loop, args=("alertmanager", _push_alert, 15.0), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
