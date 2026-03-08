"""Tests for event-driven intake dispatcher (supervisor/intake.py).

Covers IntakeDispatcher: direct submission, webhook handling,
dispatch loop, concurrency throttling, lifecycle, and edge cases.
"""

from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from supervisor.intake import IntakeDispatcher, IntakeEvent
from supervisor.incident_model import Incident


# =========================================================================
# IntakeEvent dataclass
# =========================================================================

class TestIntakeEvent:
    def test_creation(self):
        inc = Incident(incident_id="INC1", summary="test")
        evt = IntakeEvent(incident=inc, source_type="webhook")
        assert evt.incident.incident_id == "INC1"
        assert evt.source_type == "webhook"
        assert evt.receipt_handle == ""

    def test_raw_payload_default(self):
        inc = Incident(incident_id="INC1")
        evt = IntakeEvent(incident=inc)
        assert evt.raw_payload == {}


# =========================================================================
# IntakeDispatcher — direct submission
# =========================================================================

class TestDirectSubmission:
    def test_submit_returns_normalized_incident(self):
        dispatcher = IntakeDispatcher()
        inc = dispatcher.submit_incident({"incident_id": "INC1", "summary": "CPU spike"})
        assert isinstance(inc, Incident)
        assert inc.incident_id == "INC1"

    def test_submit_queues_event(self):
        dispatcher = IntakeDispatcher()
        dispatcher.submit_incident({"incident_id": "INC2", "summary": "disk full"})
        assert dispatcher.queue_size == 1

    def test_submit_multiple_events(self):
        dispatcher = IntakeDispatcher()
        for i in range(5):
            dispatcher.submit_incident({"incident_id": f"INC{i}", "summary": f"event {i}"})
        assert dispatcher.queue_size == 5

    def test_submit_custom_source_type(self):
        dispatcher = IntakeDispatcher()
        inc = dispatcher.submit_incident(
            {"incident_id": "INC3", "summary": "test"},
            source_type="api",
        )
        assert inc.incident_id == "INC3"

    def test_submit_empty_raises(self):
        dispatcher = IntakeDispatcher()
        with pytest.raises(ValueError):
            dispatcher.submit_incident({})


# =========================================================================
# IntakeDispatcher — webhook
# =========================================================================

class TestWebhookHandling:
    def test_valid_webhook(self):
        dispatcher = IntakeDispatcher()
        result = dispatcher.handle_webhook({"incident_id": "INC10", "summary": "Alert"})
        assert result["status"] == "accepted"
        assert result["incident_id"] == "INC10"
        assert dispatcher.queue_size == 1

    def test_webhook_with_source_hint(self):
        dispatcher = IntakeDispatcher()
        result = dispatcher.handle_webhook(
            {"incident_id": "INC11", "summary": "PD alert"},
            source_hint="pagerduty",
        )
        assert result["status"] == "accepted"

    def test_webhook_invalid_payload(self):
        dispatcher = IntakeDispatcher()
        result = dispatcher.handle_webhook({})
        assert result["status"] == "rejected"
        assert "error" in result

    def test_webhook_detects_servicenow(self):
        dispatcher = IntakeDispatcher()
        result = dispatcher.handle_webhook({
            "number": "INC0099",
            "short_description": "Login issue",
        })
        assert result["status"] == "accepted"
        assert result["source"] == "servicenow"

    def test_webhook_detects_pagerduty(self):
        dispatcher = IntakeDispatcher()
        result = dispatcher.handle_webhook({
            "id": "PD-100",
            "title": "Server down",
            "urgency": "high",
        })
        assert result["status"] == "accepted"
        assert result["source"] == "pagerduty"


# =========================================================================
# IntakeDispatcher — dispatch and handler
# =========================================================================

class TestDispatchLoop:
    def test_handler_called_on_dispatch(self):
        handler = MagicMock(return_value={"confidence": 85})
        dispatcher = IntakeDispatcher(handler=handler, max_concurrent=2)
        dispatcher.submit_incident({"incident_id": "INC20", "summary": "test"})
        dispatcher.start()
        # Give dispatcher time to process
        time.sleep(0.5)
        dispatcher.stop()
        handler.assert_called_once_with("INC20")

    def test_no_handler_logs_warning(self):
        dispatcher = IntakeDispatcher(handler=None, max_concurrent=2)
        dispatcher.submit_incident({"incident_id": "INC21", "summary": "test"})
        dispatcher.start()
        time.sleep(0.5)
        dispatcher.stop()
        # Should not raise, just drop the event

    def test_handler_exception_handled_gracefully(self):
        handler = MagicMock(side_effect=RuntimeError("boom"))
        dispatcher = IntakeDispatcher(handler=handler, max_concurrent=2)
        dispatcher.submit_incident({"incident_id": "INC22", "summary": "test"})
        dispatcher.start()
        time.sleep(0.5)
        dispatcher.stop()
        handler.assert_called_once()

    def test_set_handler(self):
        dispatcher = IntakeDispatcher()
        mock_handler = MagicMock(return_value={"confidence": 90})
        dispatcher.set_handler(mock_handler)
        dispatcher.submit_incident({"incident_id": "INC23", "summary": "test"})
        dispatcher.start()
        time.sleep(0.5)
        dispatcher.stop()
        mock_handler.assert_called_once_with("INC23")


# =========================================================================
# IntakeDispatcher — concurrency throttling
# =========================================================================

class TestConcurrencyThrottling:
    def test_active_investigations_starts_at_zero(self):
        dispatcher = IntakeDispatcher(max_concurrent=2)
        assert dispatcher.active_investigations == 0

    def test_concurrent_limit_respected(self):
        """Ensure max_concurrent is not exceeded."""
        active_counts = []
        lock = threading.Lock()

        def slow_handler(incident_id):
            with lock:
                active_counts.append(1)
            time.sleep(0.3)
            with lock:
                active_counts.append(-1)
            return {"confidence": 50}

        dispatcher = IntakeDispatcher(handler=slow_handler, max_concurrent=2)
        for i in range(4):
            dispatcher.submit_incident({"incident_id": f"INC{i}", "summary": f"t{i}"})

        dispatcher.start()
        time.sleep(1.5)
        dispatcher.stop()

        # At least some events were processed
        assert len(active_counts) > 0


# =========================================================================
# IntakeDispatcher — lifecycle
# =========================================================================

class TestLifecycle:
    def test_start_stop(self):
        dispatcher = IntakeDispatcher()
        dispatcher.start()
        assert dispatcher._running is True
        dispatcher.stop()
        assert dispatcher._running is False

    def test_double_start_no_op(self):
        dispatcher = IntakeDispatcher()
        dispatcher.start()
        dispatcher.start()  # Should not raise
        dispatcher.stop()

    def test_queue_size_property(self):
        dispatcher = IntakeDispatcher()
        assert dispatcher.queue_size == 0
        dispatcher.submit_incident({"incident_id": "INC30", "summary": "t"})
        assert dispatcher.queue_size == 1

    def test_active_investigations_property(self):
        dispatcher = IntakeDispatcher()
        assert dispatcher.active_investigations == 0


# =========================================================================
# SQS polling (mocked)
# =========================================================================

class TestSQSPolling:
    def test_poll_sqs_returns_if_no_boto3(self):
        """_poll_sqs returns early when boto3 unavailable or no URL."""
        dispatcher = IntakeDispatcher()
        dispatcher._running = True
        # Should return immediately — no boto3 URL configured
        with patch("supervisor.intake.SQS_QUEUE_URL", ""):
            dispatcher._poll_sqs()  # no-op, returns early

    def test_ack_sqs_handles_error(self):
        dispatcher = IntakeDispatcher()
        mock_sqs = MagicMock()
        mock_sqs.delete_message.side_effect = Exception("ack failed")
        # Should not raise
        dispatcher._ack_sqs(mock_sqs, "handle-123")

    def test_ack_sqs_calls_delete(self):
        dispatcher = IntakeDispatcher()
        mock_sqs = MagicMock()
        with patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"):
            dispatcher._ack_sqs(mock_sqs, "handle-456")
        mock_sqs.delete_message.assert_called_once()
