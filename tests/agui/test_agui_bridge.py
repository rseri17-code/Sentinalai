"""Tests for AG UI bridge — supervisor to event bus integration."""
import pytest
from unittest.mock import MagicMock, patch
from supervisor.agui_bridge import AGUIBridge


class TestAGUIBridge:
    def test_bridge_emits_investigation_started(self):
        bridge = AGUIBridge()
        emitted = []

        def mock_put_threadsafe(event):
            emitted.append(event)

        mock_bus = MagicMock()
        mock_bus.put_threadsafe = mock_put_threadsafe
        bridge._bus = mock_bus
        bridge._enabled = True

        bridge.emit_investigation_started(
            investigation_id="inv-1",
            incident_id="INC-1",
            trace_id="trace-abc",
            summary="Test incident",
            severity="critical",
        )
        assert len(emitted) == 1
        assert emitted[0].event_type.value == "investigation.started"
        assert emitted[0].investigation_id == "inv-1"

    def test_bridge_sequence_increments(self):
        bridge = AGUIBridge()
        emitted = []

        def mock_put_threadsafe(event):
            emitted.append(event)

        mock_bus = MagicMock()
        mock_bus.put_threadsafe = mock_put_threadsafe
        bridge._bus = mock_bus
        bridge._enabled = True

        bridge.emit_investigation_started("inv-1", "INC-1", "trace")
        bridge.emit_incident_classified("inv-1", "INC-1", "trace", "error_spike")
        bridge.emit_playbook_selected("inv-1", "INC-1", "trace", ["LogWorker"], "error_spike")

        seqs = [e.sequence_num for e in emitted]
        assert seqs == [0, 1, 2]

    def test_bridge_disabled_does_not_emit(self):
        bridge = AGUIBridge()
        bridge._enabled = False
        bridge._bus = MagicMock()

        bridge.emit_investigation_started("inv-1", "INC-1", "trace")
        bridge._bus.put_threadsafe.assert_not_called()

    def test_bridge_exception_does_not_propagate(self):
        """Emission failures must NEVER crash agent execution."""
        bridge = AGUIBridge()
        bridge._enabled = True

        mock_bus = MagicMock()
        mock_bus.put_threadsafe.side_effect = RuntimeError("Bus dead")
        bridge._bus = mock_bus

        # Should not raise
        bridge.emit_investigation_started("inv-1", "INC-1", "trace")

    def test_redact_sensitive_params(self):
        result = AGUIBridge._redact({
            "query": "SELECT * FROM logs",
            "password": "secret123",
            "api_key": "key-abc",
            "index": "main",
        })
        assert result["query"] == "SELECT * FROM logs"
        assert result["password"] == "***REDACTED***"
        assert result["api_key"] == "***REDACTED***"
        assert result["index"] == "main"

    def test_reset_investigation_clears_counter(self):
        bridge = AGUIBridge()
        bridge._sequence_counters["inv-1"] = 42
        bridge.reset_investigation("inv-1")
        assert "inv-1" not in bridge._sequence_counters

    def test_emit_tool_called(self):
        bridge = AGUIBridge()
        emitted = []
        mock_bus = MagicMock()
        mock_bus.put_threadsafe = lambda e: emitted.append(e)
        bridge._bus = mock_bus
        bridge._enabled = True

        bridge.emit_tool_called(
            investigation_id="inv-1", incident_id="INC-1",
            trace_id="trace", worker="LogWorker", action="search_logs",
            params={"query": "error", "token": "secret"},
            receipt_id="r-123",
        )
        assert len(emitted) == 1
        payload = emitted[0].payload
        assert payload["worker"] == "LogWorker"
        assert payload["params"]["token"] == "***REDACTED***"
        assert payload["params"]["query"] == "error"

    def test_emit_circuit_breaker(self):
        bridge = AGUIBridge()
        emitted = []
        mock_bus = MagicMock()
        mock_bus.put_threadsafe = lambda e: emitted.append(e)
        bridge._bus = mock_bus
        bridge._enabled = True

        bridge.emit_circuit_breaker_tripped("inv-1", "INC-1", "trace", "MetricsWorker", 3)
        assert emitted[0].event_type.value == "circuit_breaker.tripped"
        assert emitted[0].payload["worker"] == "MetricsWorker"
