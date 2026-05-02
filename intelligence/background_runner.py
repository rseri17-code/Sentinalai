"""Background Runner — continuous pattern intelligence loop.

Started once at AGUI boot, runs forever (until shutdown event).

Loop cadence:
  Every TELEMETRY_POLL_INTERVAL_SEC (default 60s):
    1. collect_all()       — poll all MCP workers → TelemetrySnapshots
    2. slo_engine.compute_all() — recompute burn rates
    3. detector.detect_all()   — run 5 pattern algorithms
    4. For each detection above severity gate → store in PredictionStore
    5. expire_old_predictions() — close stale pending predictions

  Every PRUNE_INTERVAL_SEC (default 3600s = 1h):
    6. aggregator.prune_old_snapshots() — enforce 7-day telemetry retention
    7. run_nightly_self_improvement() — drift damping + calibrator rebuild

The runner emits AGUIEvents so the Intelligence Feed WebSocket gets
real-time updates without polling the DB.

Startup:
  from intelligence.background_runner import get_runner
  await get_runner().start()

Shutdown:
  await get_runner().stop()
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.runner")

TELEMETRY_POLL_INTERVAL_SEC = int(os.environ.get("TELEMETRY_POLL_INTERVAL_SEC", "60"))
PRUNE_INTERVAL_SEC          = int(os.environ.get("INTELLIGENCE_PRUNE_INTERVAL_SEC", "3600"))
INTELLIGENCE_ENABLED        = os.environ.get("INTELLIGENCE_ENABLED", "true").lower() in ("1", "true", "yes")
SEVERITY_GATE               = os.environ.get("INTELLIGENCE_SEVERITY_GATE", "WATCH")


class IntelligenceRunner:
    """Drives the continuous collect → detect → store loop."""

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_prune = 0.0
        self._iteration = 0

        # Lazy-initialised to avoid import-time side-effects
        self._aggregator: Any = None
        self._slo_engine: Any = None
        self._detector: Any = None
        self._store: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not INTELLIGENCE_ENABLED:
            logger.info("Pattern Intelligence disabled (INTELLIGENCE_ENABLED=false)")
            return
        if self._task and not self._task.done():
            logger.warning("IntelligenceRunner already running")
            return

        self._stop_event.clear()
        self._init_components()
        self._task = asyncio.create_task(self._loop(), name="intelligence_loop")
        logger.info(
            "Intelligence runner started (poll=%ds prune=%ds severity_gate=%s)",
            TELEMETRY_POLL_INTERVAL_SEC, PRUNE_INTERVAL_SEC, SEVERITY_GATE,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        logger.info("Intelligence runner stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._run_one_cycle)
            except Exception as exc:
                logger.exception("Intelligence loop iteration failed: %s", exc)

            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, TELEMETRY_POLL_INTERVAL_SEC - elapsed)
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.ensure_future(self._stop_event.wait())),
                    timeout=sleep_for,
                )
                break   # stop_event fired
            except asyncio.TimeoutError:
                pass    # normal — continue loop

    def _run_one_cycle(self) -> None:
        self._iteration += 1
        logger.debug("Intelligence cycle #%d", self._iteration)

        # 1. Collect telemetry
        try:
            snapshots = self._aggregator.collect_all()
            services = list({s.service for s in snapshots}) if snapshots else self._aggregator.get_monitored_services()
        except Exception as exc:
            logger.warning("Telemetry collection failed: %s", exc)
            services = []

        # 2. Compute SLO statuses
        slo_statuses = []
        try:
            slo_statuses = self._slo_engine.compute_all(self._aggregator)
        except Exception as exc:
            logger.warning("SLO computation failed: %s", exc)

        # 3. Run pattern detection
        detections = []
        try:
            detections = self._detector.detect_all(services, self._aggregator, slo_statuses)
        except Exception as exc:
            logger.warning("Pattern detection failed: %s", exc)

        # 4. Store detections as predictions
        published = 0
        for detection in detections:
            try:
                baseline_ready = self._aggregator.is_baseline_ready(detection.service)
                pred = self._store.store(detection, baseline_ready=baseline_ready)
                if pred:
                    published += 1
                    self._emit_prediction_event(pred)
            except Exception as exc:
                logger.debug("Store prediction failed: %s", exc)

        # 5. Expire stale predictions
        try:
            expired = self._store.expire_old_predictions()
            if expired:
                logger.debug("Expired %d stale predictions", expired)
        except Exception as exc:
            logger.debug("Expire predictions failed: %s", exc)

        if detections or published:
            logger.info(
                "Intelligence cycle #%d: %d detections, %d published, %d SLO statuses, %d services",
                self._iteration, len(detections), published, len(slo_statuses), len(services),
            )

        # 6. Hourly prune + self-improvement
        now = time.time()
        if now - self._last_prune > PRUNE_INTERVAL_SEC:
            self._last_prune = now
            self._run_hourly_maintenance()

    def _run_hourly_maintenance(self) -> None:
        try:
            deleted = self._aggregator.prune_old_snapshots()
            logger.info("Telemetry prune: %d snapshots removed", deleted)
        except Exception as exc:
            logger.debug("Telemetry prune failed: %s", exc)

        try:
            from supervisor.learning_loop import run_nightly_self_improvement
            run_nightly_self_improvement()
        except Exception as exc:
            logger.debug("Self-improvement run failed: %s", exc)

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------

    def _init_components(self) -> None:
        from intelligence.telemetry_aggregator import TelemetryAggregator
        from intelligence.slo_engine import SLOEngine
        from intelligence.pattern_detector import PatternDetector
        from intelligence.prediction_store import PredictionStore

        self._aggregator  = TelemetryAggregator()
        self._slo_engine  = SLOEngine()
        self._detector    = PatternDetector()
        self._store       = PredictionStore()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _emit_prediction_event(self, pred: Any) -> None:
        try:
            import asyncio as _asyncio
            from agui.event_bus import get_bus
            from agui.schemas.events import AGUIEvent, EventType

            event = AGUIEvent(
                event_type=EventType.INTELLIGENCE_PREDICTION,
                investigation_id="pattern_intelligence",
                incident_id=pred.service,
                payload={
                    "prediction_id": pred.prediction_id,
                    "service": pred.service,
                    "pattern_type": pred.pattern_type,
                    "severity": pred.severity,
                    "confidence": round(pred.confidence, 3),
                    "metric": pred.metric,
                    "explanation": pred.explanation,
                    "predicted_breach_hours": pred.predicted_breach_hours,
                },
            )
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(get_bus().publish(event))
        except Exception as exc:
            logger.debug("Emit prediction event failed: %s", exc)

    # ------------------------------------------------------------------
    # Read-only accessors (for API endpoints)
    # ------------------------------------------------------------------

    def get_active_predictions(self, min_severity: str = "WATCH") -> list:
        if self._store is None:
            return []
        return self._store.get_active_predictions(min_severity)

    def get_slo_statuses(self) -> list:
        if self._slo_engine is None or self._aggregator is None:
            return []
        try:
            return self._slo_engine.compute_all(self._aggregator)
        except Exception:
            return []

    def get_accuracy_report(self) -> dict:
        if self._store is None:
            return {}
        return self._store.get_accuracy_report()

    def mark_false_positive(self, prediction_id: str, reason: str = "") -> bool:
        if self._store is None:
            return False
        return self._store.mark_false_positive(prediction_id, reason)

    def record_outcome(self, service: str, incident_id: str, pattern_type: str = "") -> int:
        if self._store is None:
            return 0
        return self._store.record_outcome(service, incident_id, pattern_type)


# Module-level singleton
_runner: IntelligenceRunner | None = None


def get_runner() -> IntelligenceRunner:
    global _runner
    if _runner is None:
        _runner = IntelligenceRunner()
    return _runner
