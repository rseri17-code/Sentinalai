"""Sentinel Loop — Proactive pre-incident signal detection.

Runs as a background daemon thread, polling configured services every
SENTINEL_POLL_INTERVAL_SECONDS (default 60s). For each service it:

  1. Pulls current metric snapshots from MetricsWorker
  2. Runs predictive_detector.detect_predictive_alerts()
  3. Posts WATCH/WARNING/IMMINENT signals to Slack *before* PagerDuty fires
  4. Creates an incident record for BREACHED signals

Circuit breakers per service prevent alert storms:
  - Same alert type for same service silenced for ALERT_COOLDOWN_SECONDS (default 300s)

Configure via env vars:
  SENTINEL_ENABLED=true
  SENTINEL_POLL_INTERVAL_SECONDS=60
  SENTINEL_SERVICES=payment-service,cart-service,order-db
  SENTINEL_ALERT_COOLDOWN_SECONDS=300
  SENTINEL_MIN_URGENCY=WATCH              (WATCH|WARNING|IMMINENT|BREACHED)
  SENTINEL_SLACK_CHANNEL=#sre-intelligence
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.sentinel_loop")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SENTINEL_ENABLED = os.getenv("SENTINEL_ENABLED", "true").lower() == "true"
POLL_INTERVAL = int(os.getenv("SENTINEL_POLL_INTERVAL_SECONDS", "60"))
ALERT_COOLDOWN = int(os.getenv("SENTINEL_ALERT_COOLDOWN_SECONDS", "300"))
_RAW_SERVICES = os.getenv("SENTINEL_SERVICES", "")
WATCHED_SERVICES: list[str] = [s.strip() for s in _RAW_SERVICES.split(",") if s.strip()]
SENTINEL_CHANNEL = os.getenv("SENTINEL_SLACK_CHANNEL", "#sre-intelligence")

_URGENCY_RANK = {"WATCH": 0, "WARNING": 1, "IMMINENT": 2, "BREACHED": 3}
MIN_URGENCY = os.getenv("SENTINEL_MIN_URGENCY", "WATCH").upper()
MIN_URGENCY_RANK = _URGENCY_RANK.get(MIN_URGENCY, 0)


# ---------------------------------------------------------------------------
# Alert deduplication state
# ---------------------------------------------------------------------------

@dataclass
class _AlertRecord:
    service: str
    metric_name: str
    urgency: str
    first_seen: float = field(default_factory=time.monotonic)
    last_posted: float = field(default_factory=time.monotonic)
    post_count: int = 0


class _AlertRegistry:
    """Thread-safe in-memory registry for deduplication and cooldown tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _AlertRecord] = {}   # key = service|metric

    def _key(self, service: str, metric: str) -> str:
        return f"{service}|{metric}"

    def should_post(self, service: str, metric: str, urgency: str) -> bool:
        key = self._key(service, metric)
        now = time.monotonic()
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return True
            elapsed = now - rec.last_posted
            # Always post on urgency escalation
            if _URGENCY_RANK.get(urgency, 0) > _URGENCY_RANK.get(rec.urgency, 0):
                return True
            # Respect cooldown
            return elapsed >= ALERT_COOLDOWN

    def record(self, service: str, metric: str, urgency: str) -> None:
        key = self._key(service, metric)
        now = time.monotonic()
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                self._records[key] = _AlertRecord(
                    service=service, metric_name=metric, urgency=urgency
                )
            else:
                rec.last_posted = now
                rec.urgency = urgency
                rec.post_count += 1

    def clear_resolved(self, service: str, metric: str) -> None:
        key = self._key(service, metric)
        with self._lock:
            self._records.pop(key, None)


# ---------------------------------------------------------------------------
# Sentinel Loop
# ---------------------------------------------------------------------------

class SentinelLoop:
    """Daemon thread that polls services and posts pre-incident alerts."""

    def __init__(
        self,
        services: list[str] | None = None,
        poll_interval: int = POLL_INTERVAL,
        slack_channel: str = "",
        metrics_worker: Any = None,
    ) -> None:
        self._services = services or WATCHED_SERVICES
        self._interval = poll_interval
        self._channel = slack_channel or SENTINEL_CHANNEL
        self._metrics_worker = metrics_worker
        self._registry = _AlertRegistry()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_count = 0
        self._alerts_posted = 0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.warning("Sentinel loop already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sentinalai-sentinel",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Sentinel loop started — watching %d service(s), poll every %ds",
            len(self._services),
            self._interval,
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info(
            "Sentinel loop stopped after %d cycles, %d alerts posted",
            self._cycle_count,
            self._alerts_posted,
        )

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stats(self) -> dict:
        return {
            "running": self.is_running(),
            "services_watched": len(self._services),
            "poll_interval_seconds": self._interval,
            "cycles_completed": self._cycle_count,
            "alerts_posted": self._alerts_posted,
        }

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_cycle()
            except Exception:
                logger.exception("Sentinel loop cycle failed")
            self._stop_event.wait(timeout=self._interval)

    def _poll_cycle(self) -> None:
        self._cycle_count += 1
        logger.debug("Sentinel cycle #%d — %d services", self._cycle_count, len(self._services))

        for service in self._services:
            try:
                self._check_service(service)
            except Exception:
                logger.exception("Error checking service %s", service)

    def _check_service(self, service: str) -> None:
        """Fetch current metrics and run predictive detection for one service."""
        metrics = self._fetch_metrics(service)
        if not metrics:
            logger.debug("No metrics for %s — skipping", service)
            return

        from supervisor.predictive_detector import detect_predictive_alerts
        alerts = detect_predictive_alerts(
            service=service,
            metrics_snapshot=metrics,
            lookback_minutes=30,
            threshold_multiplier=0.90,
        )

        for alert in alerts:
            urgency = alert.urgency.name if hasattr(alert.urgency, "name") else str(alert.urgency)
            rank = _URGENCY_RANK.get(urgency, 0)

            if rank < MIN_URGENCY_RANK:
                continue

            if not self._registry.should_post(service, alert.metric_name, urgency):
                logger.debug("Alert suppressed (cooldown): %s %s %s", service, alert.metric_name, urgency)
                continue

            self._post_alert(service, alert, urgency)
            self._registry.record(service, alert.metric_name, urgency)

            if urgency == "BREACHED":
                self._create_incident(service, alert)

    def _fetch_metrics(self, service: str) -> dict:
        """Fetch current metric snapshot. Uses MetricsWorker if available, else stubs."""
        if self._metrics_worker is not None:
            try:
                result = self._metrics_worker.execute(
                    "query_metrics",
                    {
                        "service": service,
                        "metrics": [
                            "cpu_utilisation",
                            "memory_utilisation",
                            "error_rate",
                            "p95_latency_ms",
                            "connection_pool_utilisation",
                            "gc_pause_ms",
                        ],
                        "window": "30m",
                    },
                )
                return result.get("metrics", {})
            except Exception as exc:
                logger.debug("MetricsWorker error for %s: %s", service, exc)

        # Graceful degradation: return empty dict — predictive detector handles missing data
        return {}

    def _post_alert(self, service: str, alert: Any, urgency: str) -> None:
        from supervisor.slack_bot import SlackFormatter, get_bot

        minutes_to_breach: float | None = None
        if hasattr(alert, "minutes_to_breach"):
            minutes_to_breach = alert.minutes_to_breach

        msg = SlackFormatter.proactive_alert(
            service=service,
            metric_name=getattr(alert, "metric_name", "unknown"),
            current_value=getattr(alert, "current_value", 0.0),
            threshold=getattr(alert, "threshold", 100.0),
            urgency=urgency,
            trend_direction=getattr(alert, "trend_direction", "rising"),
            minutes_to_breach=minutes_to_breach,
            recommended_action=getattr(alert, "recommended_action", ""),
            channel=self._channel,
        )
        result = get_bot().post(msg)
        if result.get("ok"):
            self._alerts_posted += 1
            logger.info(
                "[%s] Alert posted: %s %s (%.2f > %.2f)",
                urgency,
                service,
                getattr(alert, "metric_name", "?"),
                getattr(alert, "current_value", 0),
                getattr(alert, "threshold", 0),
            )

    def _create_incident(self, service: str, alert: Any) -> None:
        """Create a pre-incident record in the ops system for BREACHED signals."""
        try:
            import httpx
            agui_url = os.getenv("AGUI_BASE_URL", "http://localhost:8081")
            payload = {
                "incident_id": f"SENTINEL-{service}-{int(time.time())}",
                "source": "sentinel_loop",
                "service": service,
                "metric": getattr(alert, "metric_name", "unknown"),
                "urgency": "BREACHED",
                "description": (
                    f"Sentinel loop detected BREACHED threshold for {service}: "
                    f"{getattr(alert, 'metric_name', 'unknown')} = "
                    f"{getattr(alert, 'current_value', 0):.2f}"
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            httpx.post(f"{agui_url}/api/v1/intake/incidents", json=payload, timeout=5)
            logger.info("Created sentinel incident for BREACHED signal: %s %s", service, getattr(alert, "metric_name", "?"))
        except Exception as exc:
            logger.debug("Failed to create sentinel incident: %s", exc)


# ---------------------------------------------------------------------------
# Public API for snapshot-based prediction (no loop required)
# ---------------------------------------------------------------------------

def run_prediction_for_service(
    service: str,
    metrics_snapshot: dict,
    channel: str = "",
    post_to_slack: bool = True,
) -> list[dict]:
    """One-shot prediction for a service — used by the /sre predict slash command."""
    try:
        from supervisor.predictive_detector import detect_predictive_alerts
        alerts = detect_predictive_alerts(
            service=service,
            metrics_snapshot=metrics_snapshot,
            lookback_minutes=30,
            threshold_multiplier=0.90,
        )
    except Exception as exc:
        logger.exception("Prediction failed for %s: %s", service, exc)
        return []

    results = []
    for alert in alerts:
        urgency = alert.urgency.name if hasattr(alert.urgency, "name") else str(alert.urgency)
        entry = {
            "service": service,
            "metric_name": getattr(alert, "metric_name", "unknown"),
            "current_value": getattr(alert, "current_value", 0.0),
            "threshold": getattr(alert, "threshold", 100.0),
            "urgency": urgency,
            "trend_direction": getattr(alert, "trend_direction", "rising"),
            "minutes_to_breach": getattr(alert, "minutes_to_breach", None),
            "recommended_action": getattr(alert, "recommended_action", ""),
            "confidence": getattr(alert, "confidence", 0.0),
        }
        results.append(entry)

    if post_to_slack and results and channel:
        from supervisor.slack_bot import SlackFormatter, get_bot
        for entry in results[:3]:
            msg = SlackFormatter.proactive_alert(**{**entry, "channel": channel})
            get_bot().post(msg)

    return results


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_loop: SentinelLoop | None = None


def get_sentinel_loop() -> SentinelLoop:
    global _loop
    if _loop is None:
        _loop = SentinelLoop()
    return _loop


def start_sentinel_loop(services: list[str] | None = None) -> SentinelLoop:
    """Start the global sentinel loop. Call once at application startup."""
    loop = get_sentinel_loop()
    if services:
        loop._services = services
    if not loop.is_running() and SENTINEL_ENABLED and loop._services:
        loop.start()
    return loop
