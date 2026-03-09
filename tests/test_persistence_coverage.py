"""Tests for database/persistence.py — covers persist/load paths with mocked engine."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from database.persistence import (
    persist_investigation,
    persist_tool_usage,
    persist_knowledge_entry,
    load_investigation,
)


# ---------------------------------------------------------------------------
# persist_investigation
# ---------------------------------------------------------------------------

class TestPersistInvestigation:
    """Tests for persist_investigation."""

    def test_returns_none_when_engine_unavailable(self):
        with patch("database.persistence.get_engine", return_value=None):
            result = persist_investigation(
                incident_id="INC001",
                root_cause="disk full",
                confidence=85.0,
                reasoning="logs show disk at 100%",
                evidence_timeline=[{"source": "logs"}],
                tools_used=[{"tool": "log_worker"}],
            )
        assert result is None

    def test_inserts_and_returns_id_on_success(self):
        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(return_value=42)
        mock_result = MagicMock()
        mock_result.fetchone.return_value = mock_row

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_investigation(
                incident_id="INC002",
                root_cause="memory leak",
                confidence=72.5,
                reasoning="heap growing",
                evidence_timeline=[{"source": "metrics"}],
                tools_used=[{"tool": "apm_worker"}],
                elapsed_seconds=5.2,
                incident_type="memory_pressure",
                service="api-gateway",
            )

        assert result == 42
        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_returns_none_when_no_row_returned(self):
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_investigation(
                incident_id="INC003",
                root_cause="timeout",
                confidence=60.0,
                reasoning="slow queries",
                evidence_timeline=[],
                tools_used=[],
            )

        assert result is None

    def test_returns_none_on_db_exception(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("connection refused")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_investigation(
                incident_id="INC004",
                root_cause="unknown",
                confidence=50.0,
                reasoning="unclear",
                evidence_timeline=[],
                tools_used=[],
            )

        assert result is None


# ---------------------------------------------------------------------------
# persist_tool_usage
# ---------------------------------------------------------------------------

class TestPersistToolUsage:
    """Tests for persist_tool_usage."""

    def test_returns_zero_when_engine_unavailable(self):
        with patch("database.persistence.get_engine", return_value=None):
            result = persist_tool_usage(investigation_id=1, receipts=[])
        assert result == 0

    def test_inserts_all_receipts(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        receipts = [
            {"tool": "log_worker", "action": "fetch_logs", "params": {}, "status": "ok", "result_count": 10, "elapsed_ms": 200},
            {"tool": "metrics_worker", "action": "query", "params": {"query": "cpu"}, "status": "ok", "result_count": 5, "elapsed_ms": 150},
        ]

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_tool_usage(investigation_id=42, receipts=receipts)

        assert result == 2
        assert mock_conn.execute.call_count == 2
        mock_conn.commit.assert_called_once()

    def test_returns_zero_on_exception(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("insert failed")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_tool_usage(
                investigation_id=1,
                receipts=[{"tool": "x", "action": "y"}],
            )

        assert result == 0


# ---------------------------------------------------------------------------
# persist_knowledge_entry
# ---------------------------------------------------------------------------

class TestPersistKnowledgeEntry:
    """Tests for persist_knowledge_entry."""

    def test_returns_false_when_engine_unavailable(self):
        with patch("database.persistence.get_engine", return_value=None):
            result = persist_knowledge_entry(
                incident_id="INC010",
                incident_type="error_spike",
                root_cause="null pointer",
                service="auth-svc",
            )
        assert result is False

    def test_returns_true_on_success(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_knowledge_entry(
                incident_id="INC011",
                incident_type="latency",
                root_cause="slow db queries",
                service="order-svc",
                metadata={"region": "us-east-1"},
            )

        assert result is True
        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_returns_false_on_exception(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("constraint violation")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_knowledge_entry(
                incident_id="INC012",
                incident_type="error_spike",
                root_cause="bug",
                service="svc",
            )

        assert result is False


# ---------------------------------------------------------------------------
# load_investigation
# ---------------------------------------------------------------------------

class TestLoadInvestigation:
    """Tests for load_investigation."""

    def test_returns_none_when_engine_unavailable(self):
        with patch("database.persistence.get_engine", return_value=None):
            result = load_investigation("INC020")
        assert result is None

    def test_returns_none_when_not_found(self):
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = load_investigation("INC_MISSING")

        assert result is None

    def test_returns_dict_on_success(self):
        evidence = json.dumps([{"source": "logs"}])
        tools = json.dumps([{"tool": "log_worker"}])
        mock_row = (
            "INC021",      # incident_id
            "disk full",   # root_cause
            85.0,          # confidence
            "logs show",   # reasoning
            evidence,      # evidence_timeline (JSON string)
            tools,         # tools_used (JSON string)
            4.5,           # investigation_time_seconds
            "2026-01-01",  # created_at
            "2026-01-02",  # updated_at
        )

        mock_result = MagicMock()
        mock_result.fetchone.return_value = mock_row

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = load_investigation("INC021")

        assert result is not None
        assert result["incident_id"] == "INC021"
        assert result["root_cause"] == "disk full"
        assert result["confidence"] == 85.0
        assert result["evidence_timeline"] == [{"source": "logs"}]
        assert result["tools_used"] == [{"tool": "log_worker"}]
        assert result["elapsed_seconds"] == 4.5

    def test_returns_none_on_exception(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("query failed")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = load_investigation("INC022")

        assert result is None

    def test_handles_non_string_evidence_and_tools(self):
        """When evidence_timeline and tools_used are already parsed (not JSON strings)."""
        evidence_list = [{"source": "metrics", "ts": "2026-01-01"}]
        tools_list = [{"tool": "apm_worker", "action": "trace"}]
        mock_row = (
            "INC023",
            "memory leak",
            90.0,
            "heap growing",
            evidence_list,   # already a list
            tools_list,      # already a list
            7.2,
            "2026-02-01",
            "2026-02-02",
        )

        mock_result = MagicMock()
        mock_result.fetchone.return_value = mock_row

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = load_investigation("INC023")

        assert result is not None
        # Should be returned as-is without json.loads
        assert result["evidence_timeline"] is evidence_list
        assert result["tools_used"] is tools_list


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------

class TestIsEnabled:
    """Tests for is_enabled()."""

    def test_returns_false_when_engine_none(self):
        from database.persistence import is_enabled

        with patch("database.persistence.get_engine", return_value=None):
            assert is_enabled() is False

    def test_returns_true_when_engine_available(self):
        from database.persistence import is_enabled

        with patch("database.persistence.get_engine", return_value=MagicMock()):
            assert is_enabled() is True


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------

class TestPersistInvestigationEdgeCases:
    """Additional edge-case tests for persist_investigation."""

    def test_serializes_evidence_and_tools_as_json(self):
        """Verify JSON serialization of evidence_timeline and tools_used params."""
        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(return_value=99)
        mock_result = MagicMock()
        mock_result.fetchone.return_value = mock_row

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        evidence = [{"ts": "2026-01-01", "event": "spike"}]
        tools = [{"tool": "kubectl", "action": "logs"}]

        with patch("database.persistence.get_engine", return_value=mock_engine):
            persist_investigation(
                incident_id="INC-SER",
                root_cause="CPU",
                confidence=0.7,
                reasoning="High CPU",
                evidence_timeline=evidence,
                tools_used=tools,
                elapsed_seconds=5.0,
            )

        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert params["evidence_timeline"] == json.dumps(evidence, default=str)
        assert params["tools_used"] == json.dumps(tools, default=str)
        assert params["incident_id"] == "INC-SER"
        assert params["confidence"] == 0.7
        assert params["elapsed"] == 5.0

    def test_logs_info_on_success(self):
        mock_row = MagicMock()
        mock_row.__getitem__ = MagicMock(return_value=7)
        mock_result = MagicMock()
        mock_result.fetchone.return_value = mock_row

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine), \
             patch("database.persistence.logger") as mock_logger:
            persist_investigation(
                incident_id="INC-LOG",
                root_cause="test",
                confidence=0.5,
                reasoning="test",
                evidence_timeline=[],
                tools_used=[],
            )
            mock_logger.info.assert_called_once()
            assert "INC-LOG" in str(mock_logger.info.call_args)

    def test_logs_warning_on_failure(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("boom")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine), \
             patch("database.persistence.logger") as mock_logger:
            persist_investigation(
                incident_id="INC-FAIL",
                root_cause="x",
                confidence=0.0,
                reasoning="x",
                evidence_timeline=[],
                tools_used=[],
            )
            mock_logger.warning.assert_called_once()
            assert "INC-FAIL" in str(mock_logger.warning.call_args)


class TestPersistToolUsageEdgeCases:
    """Additional edge-case tests for persist_tool_usage."""

    def test_empty_receipts_returns_zero_and_commits(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_tool_usage(investigation_id=1, receipts=[])

        assert result == 0
        mock_conn.execute.assert_not_called()
        mock_conn.commit.assert_called_once()

    def test_handles_missing_receipt_fields(self):
        """Receipt with no keys should use defaults from .get() calls."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            result = persist_tool_usage(investigation_id=1, receipts=[{}])

        assert result == 1
        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert params["tool"] == "."
        assert params["params"] == json.dumps({})
        assert params["duration"] == 0
        response_data = json.loads(params["response"])
        assert response_data["status"] == ""
        assert response_data["result_count"] == 0

    def test_formats_tool_name_correctly(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            persist_tool_usage(
                investigation_id=1,
                receipts=[{"tool": "kubectl", "action": "logs", "elapsed_ms": 50}],
            )

        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert params["tool"] == "kubectl.logs"
        assert params["duration"] == 50

    def test_logs_warning_on_failure(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("db error")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine), \
             patch("database.persistence.logger") as mock_logger:
            persist_tool_usage(investigation_id=1, receipts=[{"tool": "x"}])
            mock_logger.warning.assert_called_once()


class TestPersistKnowledgeEntryEdgeCases:
    """Additional edge-case tests for persist_knowledge_entry."""

    def test_serializes_metadata_as_json(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        meta = {"severity": "critical", "team": "platform"}
        with patch("database.persistence.get_engine", return_value=mock_engine):
            persist_knowledge_entry(
                incident_id="INC-META",
                incident_type="infra",
                root_cause="CPU throttle",
                service="compute",
                metadata=meta,
            )

        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert params["metadata"] == json.dumps(meta, default=str)
        assert params["incident_id"] == "INC-META"
        assert params["service"] == "compute"

    def test_defaults_metadata_to_empty_dict_when_none(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            persist_knowledge_entry(
                incident_id="INC-NONE-META",
                incident_type="app",
                root_cause="timeout",
                service="gateway",
                metadata=None,
            )

        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert params["metadata"] == json.dumps({})

    def test_logs_info_on_success(self):
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine), \
             patch("database.persistence.logger") as mock_logger:
            persist_knowledge_entry(
                incident_id="INC-KL",
                incident_type="infra",
                root_cause="test",
                service="test",
            )
            mock_logger.info.assert_called_once()
            assert "INC-KL" in str(mock_logger.info.call_args)

    def test_logs_warning_on_failure(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("boom")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine), \
             patch("database.persistence.logger") as mock_logger:
            persist_knowledge_entry(
                incident_id="INC-KL-FAIL",
                incident_type="app",
                root_cause="x",
                service="x",
            )
            mock_logger.warning.assert_called_once()
            assert "INC-KL-FAIL" in str(mock_logger.warning.call_args)


class TestLoadInvestigationEdgeCases:
    """Additional edge-case tests for load_investigation."""

    def test_passes_incident_id_as_parameter(self):
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine):
            load_investigation("INC-PARAM-CHECK")

        call_args = mock_conn.execute.call_args
        params = call_args[0][1]
        assert params["incident_id"] == "INC-PARAM-CHECK"

    def test_logs_warning_on_failure(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("db down")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn

        with patch("database.persistence.get_engine", return_value=mock_engine), \
             patch("database.persistence.logger") as mock_logger:
            load_investigation("INC-LOG-FAIL")
            mock_logger.warning.assert_called_once()
            assert "INC-LOG-FAIL" in str(mock_logger.warning.call_args)
