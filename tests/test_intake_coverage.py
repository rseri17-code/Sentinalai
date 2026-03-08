"""Additional tests for supervisor/intake.py — SQS-related coverage.

Covers previously uncovered lines:
- Line 48:      boto3 import success path (_BOTO3_AVAILABLE = True)
- Lines 158-200: _poll_sqs() SQS polling logic
- Lines 259-261: SQS ack in _handle_event()
- Lines 292-297: SQS thread start in start()
- Line 305:     SQS thread join in stop()
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from supervisor.intake import IntakeDispatcher, IntakeEvent
from supervisor.incident_model import Incident


def _inject_boto3_mock():
    """Create a mock boto3 and inject it into supervisor.intake module."""
    mock_boto3 = MagicMock()
    import supervisor.intake as intake_mod
    intake_mod.boto3 = mock_boto3
    return mock_boto3, intake_mod


def _cleanup_boto3(intake_mod, mock_boto3):
    if hasattr(intake_mod, 'boto3') and intake_mod.boto3 is mock_boto3:
        delattr(intake_mod, 'boto3')


# =========================================================================
# _poll_sqs() method (lines 153-200)
# =========================================================================

class TestPollSQS:
    def _make_sqs_message(self, payload: dict, receipt: str = "r-1") -> dict:
        return {
            "Body": json.dumps(payload),
            "ReceiptHandle": receipt,
        }

    def test_poll_sqs_returns_early_no_boto3(self):
        dispatcher = IntakeDispatcher()
        dispatcher._running = True
        with patch("supervisor.intake._BOTO3_AVAILABLE", False):
            dispatcher._poll_sqs()

    def test_poll_sqs_returns_early_no_url(self):
        dispatcher = IntakeDispatcher()
        dispatcher._running = True
        with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
             patch("supervisor.intake.SQS_QUEUE_URL", ""):
            dispatcher._poll_sqs()

    def test_poll_sqs_receives_valid_message(self):
        payload = {"incident_id": "INC-SQS-1", "summary": "disk full"}
        msg = self._make_sqs_message(payload, "receipt-abc")

        mock_boto3, intake_mod = _inject_boto3_mock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        call_count = 0
        dispatcher = IntakeDispatcher()
        dispatcher._running = True

        def receive_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"Messages": [msg]}
            dispatcher._running = False
            return {"Messages": []}

        mock_client.receive_message.side_effect = receive_side_effect

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch("supervisor.intake.time"):
                dispatcher._poll_sqs()

            assert dispatcher.queue_size == 1
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)

    def test_poll_sqs_invalid_json_skipped(self):
        msg = {"Body": "not-json{{", "ReceiptHandle": "receipt-bad"}

        mock_boto3, intake_mod = _inject_boto3_mock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        call_count = 0
        dispatcher = IntakeDispatcher()
        dispatcher._running = True

        def receive_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"Messages": [msg]}
            dispatcher._running = False
            return {"Messages": []}

        mock_client.receive_message.side_effect = receive_side_effect

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch("supervisor.intake.time"):
                dispatcher._poll_sqs()

            mock_client.delete_message.assert_called_once()
            assert dispatcher.queue_size == 0
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)

    def test_poll_sqs_empty_response_sleeps(self):
        mock_boto3, intake_mod = _inject_boto3_mock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        call_count = 0
        dispatcher = IntakeDispatcher()
        dispatcher._running = True

        def receive_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                dispatcher._running = False
            return {"Messages": []}

        mock_client.receive_message.side_effect = receive_side_effect
        mock_time = MagicMock()

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch("supervisor.intake.time", mock_time):
                dispatcher._poll_sqs()

            mock_time.sleep.assert_called()
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)

    def test_poll_sqs_exception_sleeps_double_interval(self):
        mock_boto3, intake_mod = _inject_boto3_mock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        call_count = 0
        dispatcher = IntakeDispatcher()
        dispatcher._running = True

        def receive_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network fail")
            dispatcher._running = False
            return {"Messages": []}

        mock_client.receive_message.side_effect = receive_side_effect
        mock_time = MagicMock()

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch("supervisor.intake.time", mock_time), \
                 patch("supervisor.intake.SQS_POLL_INTERVAL", 5):
                dispatcher._poll_sqs()

            mock_time.sleep.assert_any_call(10)
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)

    def test_poll_sqs_multiple_messages_in_batch(self):
        msg1 = self._make_sqs_message({"incident_id": "INC-A", "summary": "a"}, "r-a")
        msg2 = self._make_sqs_message({"incident_id": "INC-B", "summary": "b"}, "r-b")

        mock_boto3, intake_mod = _inject_boto3_mock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        call_count = 0
        dispatcher = IntakeDispatcher()
        dispatcher._running = True

        def receive_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"Messages": [msg1, msg2]}
            dispatcher._running = False
            return {"Messages": []}

        mock_client.receive_message.side_effect = receive_side_effect

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch("supervisor.intake.time"):
                dispatcher._poll_sqs()

            assert dispatcher.queue_size == 2
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)


# =========================================================================
# SQS ack in _handle_event() (lines 259-261)
# =========================================================================

class TestHandleEventSQSAck:
    def test_handle_event_acks_sqs_on_success(self):
        mock_boto3, intake_mod = _inject_boto3_mock()
        mock_sqs_client = MagicMock()
        mock_boto3.client.return_value = mock_sqs_client

        handler = MagicMock(return_value={"confidence": 95})
        dispatcher = IntakeDispatcher(handler=handler)

        incident = Incident(incident_id="INC-ACK-1", summary="ack test")
        event = IntakeEvent(
            incident=incident,
            source_type="sqs",
            receipt_handle="receipt-ack-1",
        )

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"):
                dispatcher._handle_event(event)

            handler.assert_called_once_with("INC-ACK-1")
            mock_sqs_client.delete_message.assert_called_once()
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)

    def test_handle_event_no_ack_for_non_sqs(self):
        handler = MagicMock(return_value={"confidence": 80})
        dispatcher = IntakeDispatcher(handler=handler)

        incident = Incident(incident_id="INC-NOSQS", summary="webhook event")
        event = IntakeEvent(
            incident=incident,
            source_type="webhook",
            receipt_handle="",
        )

        with patch("supervisor.intake._BOTO3_AVAILABLE", False):
            dispatcher._handle_event(event)

        handler.assert_called_once()

    def test_handle_event_no_ack_when_no_receipt_handle(self):
        handler = MagicMock(return_value={"confidence": 80})
        dispatcher = IntakeDispatcher(handler=handler)

        incident = Incident(incident_id="INC-NORH", summary="no receipt")
        event = IntakeEvent(
            incident=incident,
            source_type="sqs",
            receipt_handle="",
        )

        with patch("supervisor.intake._BOTO3_AVAILABLE", False):
            dispatcher._handle_event(event)

        handler.assert_called_once()

    def test_handle_event_decrements_active_count(self):
        handler = MagicMock(return_value={"confidence": 70})
        dispatcher = IntakeDispatcher(handler=handler)

        incident = Incident(incident_id="INC-DEC", summary="decrement")
        event = IntakeEvent(incident=incident, source_type="direct")

        dispatcher._active_count = 1
        with patch("supervisor.intake._BOTO3_AVAILABLE", False):
            dispatcher._handle_event(event)

        assert dispatcher._active_count == 0


# =========================================================================
# SQS thread start in start() (lines 292-297)
# =========================================================================

class TestStartSQSThread:
    def test_start_creates_sqs_thread_when_configured(self):
        mock_boto3, intake_mod = _inject_boto3_mock()
        dispatcher = IntakeDispatcher()

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch.object(dispatcher, "_poll_sqs"), \
                 patch.object(dispatcher, "_dispatch_loop"):
                dispatcher.start()
                assert dispatcher._sqs_thread is not None
                assert dispatcher._sqs_thread.name == "intake-sqs-poller"
                dispatcher.stop()
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)

    def test_start_no_sqs_thread_when_no_url(self):
        dispatcher = IntakeDispatcher()

        with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
             patch("supervisor.intake.SQS_QUEUE_URL", ""), \
             patch.object(dispatcher, "_dispatch_loop"):
            dispatcher.start()
            assert dispatcher._sqs_thread is None
            dispatcher.stop()

    def test_start_no_sqs_thread_when_no_boto3(self):
        dispatcher = IntakeDispatcher()

        with patch("supervisor.intake._BOTO3_AVAILABLE", False), \
             patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
             patch.object(dispatcher, "_dispatch_loop"):
            dispatcher.start()
            assert dispatcher._sqs_thread is None
            dispatcher.stop()


# =========================================================================
# SQS thread join in stop() (line 305)
# =========================================================================

class TestStopSQSThread:
    def test_stop_joins_sqs_thread(self):
        dispatcher = IntakeDispatcher()
        mock_sqs_thread = MagicMock()
        mock_worker_thread = MagicMock()

        dispatcher._running = True
        dispatcher._sqs_thread = mock_sqs_thread
        dispatcher._worker_thread = mock_worker_thread

        dispatcher.stop()

        assert dispatcher._running is False
        mock_worker_thread.join.assert_called_once_with(timeout=5)
        mock_sqs_thread.join.assert_called_once_with(timeout=5)

    def test_stop_without_sqs_thread(self):
        dispatcher = IntakeDispatcher()
        dispatcher._running = True
        dispatcher._worker_thread = MagicMock()
        dispatcher._sqs_thread = None

        dispatcher.stop()

        assert dispatcher._running is False

    def test_full_lifecycle_with_sqs(self):
        mock_boto3, intake_mod = _inject_boto3_mock()
        dispatcher = IntakeDispatcher()

        try:
            with patch("supervisor.intake._BOTO3_AVAILABLE", True), \
                 patch("supervisor.intake.SQS_QUEUE_URL", "https://sqs.example.com/q"), \
                 patch.object(dispatcher, "_poll_sqs"), \
                 patch.object(dispatcher, "_dispatch_loop"):
                dispatcher.start()
                assert dispatcher._running is True
                assert dispatcher._sqs_thread is not None
                dispatcher.stop()
                assert dispatcher._running is False
        finally:
            _cleanup_boto3(intake_mod, mock_boto3)
