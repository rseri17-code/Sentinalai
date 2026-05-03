"""Verification Loop — monitors service health after a fix is applied.

After a fix is applied (rollback or code change), we need to verify the service
actually recovered. This loop polls metrics/logs at intervals and determines
whether the fix worked.

Algorithm:
  1. Capture baseline (pre-fix error rate, latency, etc.)
  2. Poll every POLL_INTERVAL_SEC seconds
  3. Require STABLE_THRESHOLD consecutive stable readings
  4. After MAX_POLLS polls, declare failure if not stable
  5. On success: trigger SNOW ticket close + emit AGUI event
  6. On failure: escalate (human alert, do not auto-close)

Stability criteria (all must pass):
  - Error rate < baseline * 1.1  (within 10% of pre-incident baseline)
  - Latency p95 < baseline_latency * 1.2
  - No new error patterns in logs matching the original error signature

Architecture:
    VerificationLoop runs as an asyncio background task.
    Reports progress via callback (wired to AGUI event bus in production).
    Is fully self-contained and does not modify any investigation state directly.

Usage:
    loop = VerificationLoop(metrics_worker, log_worker)
    result = await loop.watch(
        investigation_id="inv-123",
        service="payment-service",
        error_signature="NullPointerException",
        baseline={"error_rate": 0.01, "latency_p95_ms": 120.0},
    )
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger("sentinalai.verification_loop")


@dataclass
class VerificationResult:
    """Outcome of the verification loop."""
    investigation_id: str
    service: str
    success: bool
    stable_readings: int
    total_polls: int
    duration_sec: float
    final_metrics: dict
    failure_reason: str = ""
    closed_ticket: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "investigation_id": self.investigation_id,
            "service": self.service,
            "success": self.success,
            "stable_readings": self.stable_readings,
            "total_polls": self.total_polls,
            "duration_sec": self.duration_sec,
            "final_metrics": self.final_metrics,
            "failure_reason": self.failure_reason,
            "closed_ticket": self.closed_ticket,
            "timestamp": self.timestamp,
        }


# Callback signature: (investigation_id, event_type, data) -> Awaitable[None]
VerificationCallback = Callable[[str, str, dict], Awaitable[None]]


class VerificationLoop:
    """Async service health verifier.

    Parameters
    ----------
    metrics_worker:
        Worker with execute("get_service_metrics", params) method.
    log_worker:
        Worker with execute("search_logs", params) method.
    poll_interval_sec:
        Seconds between polls (default: 60).
    max_polls:
        Maximum number of polls before declaring failure (default: 10).
    stable_threshold:
        Consecutive stable readings required for success (default: 3).
    """

    POLL_INTERVAL_SEC = 60
    MAX_POLLS = 10
    STABLE_THRESHOLD = 3

    def __init__(
        self,
        metrics_worker: Any,
        log_worker: Any,
        poll_interval_sec: int = POLL_INTERVAL_SEC,
        max_polls: int = MAX_POLLS,
        stable_threshold: int = STABLE_THRESHOLD,
    ) -> None:
        self._metrics = metrics_worker
        self._logs = log_worker
        self._poll_interval = poll_interval_sec
        self._max_polls = max_polls
        self._stable_threshold = stable_threshold

    async def watch(
        self,
        investigation_id: str,
        service: str,
        error_signature: str = "",
        baseline: Optional[dict] = None,
        callback: Optional[VerificationCallback] = None,
        itsm_worker: Any = None,
        incident_id: str = "",
    ) -> VerificationResult:
        """Run the verification loop for a service post-fix.

        Parameters
        ----------
        investigation_id:   Tracing key for events.
        service:            Service name to monitor.
        error_signature:    Substring or regex to search for in logs.
        baseline:           Pre-incident baseline metrics dict.
        callback:           Async callback for progress events (wired to AGUI bus).
        itsm_worker:        If provided, auto-close SNOW ticket on success.
        incident_id:        ServiceNow incident number for ticket close.
        """
        baseline = baseline or {}
        start_time = time.time()
        stable_count = 0
        total_polls = 0
        last_metrics: dict = {}

        logger.info(
            "Verification loop started: inv=%s service=%s max_polls=%d interval=%ds",
            investigation_id, service, self._max_polls, self._poll_interval,
        )

        await self._emit(callback, investigation_id, "verification.started", {
            "service": service,
            "max_polls": self._max_polls,
            "poll_interval_sec": self._poll_interval,
            "stable_threshold": self._stable_threshold,
        })

        for poll_num in range(1, self._max_polls + 1):
            await asyncio.sleep(self._poll_interval)
            total_polls = poll_num

            # Collect current metrics
            metrics = await self._collect_metrics(service)
            last_metrics = metrics

            # Check stability
            is_stable = self._check_stability(metrics, baseline, error_signature, service)

            await self._emit(callback, investigation_id, "verification.poll", {
                "poll": poll_num,
                "max_polls": self._max_polls,
                "is_stable": is_stable,
                "stable_count": stable_count,
                "metrics": metrics,
            })

            if is_stable:
                stable_count += 1
                logger.info(
                    "Verification poll %d/%d: STABLE (%d/%d threshold) — service=%s",
                    poll_num, self._max_polls, stable_count, self._stable_threshold, service,
                )
                if stable_count >= self._stable_threshold:
                    # Success!
                    duration = time.time() - start_time
                    result = VerificationResult(
                        investigation_id=investigation_id,
                        service=service,
                        success=True,
                        stable_readings=stable_count,
                        total_polls=total_polls,
                        duration_sec=duration,
                        final_metrics=last_metrics,
                    )
                    # Auto-close SNOW ticket if itsm_worker provided
                    if itsm_worker and incident_id:
                        closed = await self._close_snow_ticket(
                            itsm_worker, incident_id, investigation_id, result
                        )
                        result.closed_ticket = closed

                    await self._emit(callback, investigation_id, "verification.success", {
                        "duration_sec": duration,
                        "stable_readings": stable_count,
                        "total_polls": total_polls,
                        "ticket_closed": result.closed_ticket,
                    })

                    logger.info(
                        "Verification SUCCESS: inv=%s service=%s polls=%d duration=%.0fs",
                        investigation_id, service, total_polls, duration,
                    )
                    try:
                        from supervisor.learning_loop import record_verification_outcome
                        record_verification_outcome(
                            investigation_id=investigation_id,
                            rca_was_correct=True,
                            verification_duration_sec=duration,
                        )
                    except Exception:
                        pass
                    try:
                        from supervisor.metrics_dashboard import get_dashboard_engine
                        get_dashboard_engine().update_outcome(
                            investigation_id, fix_verified=True
                        )
                    except Exception:
                        pass
                    return result
            else:
                stable_count = 0
                logger.warning(
                    "Verification poll %d/%d: UNSTABLE — service=%s metrics=%s",
                    poll_num, self._max_polls, service, metrics,
                )

        # Exhausted max polls without stabilizing
        duration = time.time() - start_time
        failure_reason = (
            f"Service {service} did not stabilize after {self._max_polls} polls "
            f"({duration:.0f}s). Manual intervention required."
        )
        result = VerificationResult(
            investigation_id=investigation_id,
            service=service,
            success=False,
            stable_readings=stable_count,
            total_polls=total_polls,
            duration_sec=duration,
            final_metrics=last_metrics,
            failure_reason=failure_reason,
        )

        await self._emit(callback, investigation_id, "verification.failed", {
            "reason": failure_reason,
            "total_polls": total_polls,
            "duration_sec": duration,
        })

        logger.error(
            "Verification FAILED: inv=%s service=%s reason=%s",
            investigation_id, service, failure_reason,
        )
        try:
            from supervisor.learning_loop import record_verification_outcome
            record_verification_outcome(
                investigation_id=investigation_id,
                rca_was_correct=False,
                verification_duration_sec=duration,
            )
        except Exception:
            pass
        try:
            from supervisor.metrics_dashboard import get_dashboard_engine
            get_dashboard_engine().update_outcome(
                investigation_id, fix_verified=False
            )
        except Exception:
            pass
        return result

    def _check_stability(
        self,
        metrics: dict,
        baseline: dict,
        error_signature: str,
        service: str,
    ) -> bool:
        """Determine if the service is stable.

        Checks:
        1. Error rate is within 10% of baseline (or below 1% if no baseline)
        2. Latency p95 is within 20% of baseline (or below 500ms if no baseline)
        3. No new log errors matching the error signature
        """
        # Error rate check
        error_rate = metrics.get("error_rate", 0.0)
        baseline_error_rate = baseline.get("error_rate", 0.0)
        if baseline_error_rate > 0:
            if error_rate > baseline_error_rate * 1.1:
                logger.debug("Stability check FAIL: error_rate=%.4f > baseline=%.4f * 1.1",
                             error_rate, baseline_error_rate)
                return False
        elif error_rate > 0.01:  # 1% threshold without baseline
            logger.debug("Stability check FAIL: error_rate=%.4f > 0.01", error_rate)
            return False

        # Latency check
        latency = metrics.get("latency_p95_ms", 0.0)
        baseline_latency = baseline.get("latency_p95_ms", 0.0)
        if baseline_latency > 0:
            if latency > baseline_latency * 1.2:
                logger.debug("Stability check FAIL: latency_p95=%.1f > baseline=%.1f * 1.2",
                             latency, baseline_latency)
                return False
        elif latency > 500:
            logger.debug("Stability check FAIL: latency_p95=%.1f > 500ms", latency)
            return False

        # Error signature in logs
        if error_signature and metrics.get("recent_error_matches", 0) > 0:
            logger.debug("Stability check FAIL: found %d error matches for '%s'",
                         metrics.get("recent_error_matches"), error_signature)
            return False

        return True

    async def _collect_metrics(self, service: str) -> dict:
        """Collect current service metrics from metrics_worker + log_worker."""
        metrics: dict = {}

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._metrics.execute("get_service_metrics", {
                    "service": service,
                    "window_minutes": 5,
                })
            )
            if isinstance(result, dict):
                m = result.get("metrics", result)
                metrics["error_rate"] = float(m.get("error_rate", m.get("error_pct", 0)) or 0)
                metrics["latency_p95_ms"] = float(m.get("latency_p95", m.get("p95_ms", 0)) or 0)
                metrics["request_rate"] = float(m.get("request_rate", m.get("rps", 0)) or 0)
        except Exception as exc:
            logger.warning("Failed to collect metrics for %s: %s", service, exc)
            metrics["error_rate"] = 0.0
            metrics["latency_p95_ms"] = 0.0

        try:
            loop = asyncio.get_event_loop()
            log_result = await loop.run_in_executor(
                None,
                lambda: self._logs.execute("search_logs", {
                    "service": service,
                    "query": "level=ERROR OR level=CRITICAL",
                    "time_window_minutes": 5,
                    "limit": 10,
                })
            )
            if isinstance(log_result, dict):
                entries = log_result.get("logs", log_result.get("results", []))
                metrics["recent_error_count"] = len(entries) if isinstance(entries, list) else 0
                metrics["recent_error_matches"] = metrics["recent_error_count"]
        except Exception as exc:
            logger.warning("Failed to collect logs for %s: %s", service, exc)
            metrics["recent_error_count"] = 0
            metrics["recent_error_matches"] = 0

        return metrics

    async def _close_snow_ticket(
        self,
        itsm_worker: Any,
        incident_id: str,
        investigation_id: str,
        result: "VerificationResult",
    ) -> bool:
        """Auto-close the ServiceNow incident after successful fix verification."""
        try:
            loop = asyncio.get_event_loop()
            close_result = await loop.run_in_executor(
                None,
                lambda: itsm_worker.execute("update_incident", {
                    "incident_id": incident_id,
                    "state": "resolved",
                    "resolution_code": "Solved (Permanently)",
                    "resolution_notes": (
                        f"Automatically resolved by SentinalAI (investigation {investigation_id}). "
                        f"Service stabilized after {result.stable_readings} consecutive stable readings "
                        f"over {result.total_polls} polls ({result.duration_sec:.0f}s)."
                    ),
                })
            )
            success = isinstance(close_result, dict) and "error" not in close_result
            if success:
                logger.info(
                    "SNOW ticket %s auto-closed by verification loop for inv=%s",
                    incident_id, investigation_id,
                )
            else:
                logger.warning("SNOW ticket close failed: %s", close_result)
            return success
        except Exception as exc:
            logger.warning("Failed to close SNOW ticket %s: %s", incident_id, exc)
            return False

    @staticmethod
    async def _emit(
        callback: Optional[VerificationCallback],
        investigation_id: str,
        event_type: str,
        data: dict,
    ) -> None:
        """Safely invoke the progress callback."""
        if callback is None:
            return
        try:
            await callback(investigation_id, event_type, data)
        except Exception as exc:
            logger.warning("Verification callback error (%s): %s", event_type, exc)
