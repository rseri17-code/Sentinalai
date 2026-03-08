"""Event-driven incident intake for SentinalAI.

Supports multiple intake sources:
- SQS: Polls an AWS SQS queue for incident events
- Webhook: Accepts HTTP POST from alerting systems (Moogsoft, PagerDuty, etc.)
- Direct: Programmatic submission via submit_incident()

All intake paths normalize through the canonical Incident model before
dispatching to the investigation pipeline.

Configuration via environment variables:
    INTAKE_SQS_QUEUE_URL      - SQS queue URL (enables SQS polling)
    INTAKE_SQS_POLL_INTERVAL  - Seconds between polls (default: 5)
    INTAKE_SQS_MAX_MESSAGES   - Max messages per poll (default: 5)
    INTAKE_SQS_WAIT_SECONDS   - Long poll wait time (default: 10)
    INTAKE_MAX_CONCURRENT      - Max concurrent investigations (default: 3)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Any

from supervisor.incident_model import Incident

logger = logging.getLogger("sentinalai.intake")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SQS_QUEUE_URL = os.environ.get("INTAKE_SQS_QUEUE_URL", "")
SQS_POLL_INTERVAL = int(os.environ.get("INTAKE_SQS_POLL_INTERVAL", "5"))
SQS_MAX_MESSAGES = int(os.environ.get("INTAKE_SQS_MAX_MESSAGES", "5"))
SQS_WAIT_SECONDS = int(os.environ.get("INTAKE_SQS_WAIT_SECONDS", "10"))
MAX_CONCURRENT = int(os.environ.get("INTAKE_MAX_CONCURRENT", "3"))

# Optional boto3 import
try:
    import boto3
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False


# ---------------------------------------------------------------------------
# Intake event wrapper
# ---------------------------------------------------------------------------

@dataclass
class IntakeEvent:
    """Normalized intake event ready for investigation dispatch."""

    incident: Incident
    source_type: str = "direct"  # sqs, webhook, direct
    receipt_handle: str = ""  # SQS receipt handle for ack
    raw_payload: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Intake dispatcher
# ---------------------------------------------------------------------------

class IntakeDispatcher:
    """Manages intake from multiple sources and dispatches investigations.

    Normalizes all incoming events through the Incident model and dispatches
    to a configurable handler (typically SentinalAISupervisor.investigate).
    """

    def __init__(
        self,
        handler: Callable[[str], dict] | None = None,
        max_concurrent: int = MAX_CONCURRENT,
    ):
        self._handler = handler
        self._queue: Queue[IntakeEvent] = Queue()
        self._max_concurrent = max_concurrent
        self._active_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._sqs_thread: threading.Thread | None = None

    def set_handler(self, handler: Callable[[str], dict]) -> None:
        """Set the investigation handler (e.g., supervisor.investigate)."""
        self._handler = handler

    # ------------------------------------------------------------------ #
    # Direct submission
    # ------------------------------------------------------------------ #

    def submit_incident(self, data: dict, source_type: str = "direct") -> Incident:
        """Submit an incident for investigation.

        Normalizes through the Incident model and queues for processing.
        Returns the normalized Incident.
        """
        incident = Incident.from_dict(data)
        event = IntakeEvent(
            incident=incident,
            source_type=source_type,
            raw_payload=data,
        )
        self._queue.put(event)
        logger.info(
            "Incident queued: id=%s source=%s type=%s",
            incident.incident_id, source_type, incident.source,
        )
        return incident

    # ------------------------------------------------------------------ #
    # Webhook intake
    # ------------------------------------------------------------------ #

    def handle_webhook(self, payload: dict, source_hint: str = "") -> dict:
        """Process an incoming webhook payload.

        Auto-detects source format and normalizes. Returns acknowledgment dict.
        """
        try:
            incident = Incident.from_dict(payload)
            event = IntakeEvent(
                incident=incident,
                source_type=f"webhook:{source_hint}" if source_hint else "webhook",
                raw_payload=payload,
            )
            self._queue.put(event)
            logger.info(
                "Webhook received: id=%s source=%s",
                incident.incident_id, incident.source,
            )
            return {
                "status": "accepted",
                "incident_id": incident.incident_id,
                "source": incident.source,
            }
        except (ValueError, TypeError) as exc:
            logger.warning("Webhook rejected: %s", exc)
            return {"status": "rejected", "error": str(exc)}

    # ------------------------------------------------------------------ #
    # SQS polling
    # ------------------------------------------------------------------ #

    def _poll_sqs(self) -> None:
        """Poll SQS queue for incident events (runs in background thread)."""
        if not _BOTO3_AVAILABLE or not SQS_QUEUE_URL:
            return

        sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        logger.info("SQS intake started: queue=%s", SQS_QUEUE_URL)

        while self._running:
            try:
                response = sqs.receive_message(
                    QueueUrl=SQS_QUEUE_URL,
                    MaxNumberOfMessages=SQS_MAX_MESSAGES,
                    WaitTimeSeconds=SQS_WAIT_SECONDS,
                )
                messages = response.get("Messages", [])

                for msg in messages:
                    body = msg.get("Body", "{}")
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        logger.warning("SQS message not valid JSON, skipping")
                        self._ack_sqs(sqs, msg["ReceiptHandle"])
                        continue

                    try:
                        incident = Incident.from_dict(payload)
                        event = IntakeEvent(
                            incident=incident,
                            source_type="sqs",
                            receipt_handle=msg["ReceiptHandle"],
                            raw_payload=payload,
                        )
                        self._queue.put(event)
                        logger.info(
                            "SQS message received: id=%s", incident.incident_id,
                        )
                    except (ValueError, TypeError) as exc:
                        logger.warning("SQS message rejected: %s", exc)
                        self._ack_sqs(sqs, msg["ReceiptHandle"])

                if not messages:
                    time.sleep(SQS_POLL_INTERVAL)

            except Exception as exc:
                logger.error("SQS poll error: %s", exc)
                time.sleep(SQS_POLL_INTERVAL * 2)

    def _ack_sqs(self, sqs_client: Any, receipt_handle: str) -> None:
        """Acknowledge (delete) an SQS message."""
        try:
            sqs_client.delete_message(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=receipt_handle,
            )
        except Exception as exc:
            logger.warning("SQS ack failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Dispatch loop
    # ------------------------------------------------------------------ #

    def _dispatch_loop(self) -> None:
        """Main dispatch loop — processes queued events."""
        while self._running:
            try:
                event = self._queue.get(timeout=1.0)
            except Empty:
                continue

            # Throttle concurrent investigations
            with self._lock:
                if self._active_count >= self._max_concurrent:
                    self._queue.put(event)  # Re-queue
                    time.sleep(0.5)
                    continue
                self._active_count += 1

            # Dispatch in thread
            thread = threading.Thread(
                target=self._handle_event,
                args=(event,),
                daemon=True,
            )
            thread.start()

    def _handle_event(self, event: IntakeEvent) -> None:
        """Handle a single intake event."""
        try:
            if self._handler is None:
                logger.warning("No handler configured, dropping event %s", event.incident.incident_id)
                return

            logger.info(
                "Dispatching investigation: id=%s source=%s",
                event.incident.incident_id, event.source_type,
            )
            result = self._handler(event.incident.incident_id)
            logger.info(
                "Investigation complete: id=%s confidence=%s",
                event.incident.incident_id, result.get("confidence", 0),
            )

            # Ack SQS message on success
            if event.source_type == "sqs" and event.receipt_handle:
                if _BOTO3_AVAILABLE and SQS_QUEUE_URL:
                    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
                    self._ack_sqs(sqs, event.receipt_handle)

        except Exception as exc:
            logger.error(
                "Investigation failed for %s: %s",
                event.incident.incident_id, exc,
            )
        finally:
            with self._lock:
                self._active_count -= 1

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the intake dispatcher (dispatch loop + optional SQS poller)."""
        if self._running:
            return
        self._running = True

        self._worker_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="intake-dispatcher",
        )
        self._worker_thread.start()
        logger.info("Intake dispatcher started")

        # Start SQS poller if configured
        if _BOTO3_AVAILABLE and SQS_QUEUE_URL:
            self._sqs_thread = threading.Thread(
                target=self._poll_sqs,
                daemon=True,
                name="intake-sqs-poller",
            )
            self._sqs_thread.start()

    def stop(self) -> None:
        """Stop the intake dispatcher."""
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        if self._sqs_thread:
            self._sqs_thread.join(timeout=5)
        logger.info("Intake dispatcher stopped")

    @property
    def queue_size(self) -> int:
        """Current number of queued events."""
        return self._queue.qsize()

    @property
    def active_investigations(self) -> int:
        """Number of currently running investigations."""
        with self._lock:
            return self._active_count
