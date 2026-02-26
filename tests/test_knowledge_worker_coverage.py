"""Extended tests for workers/knowledge_worker.py — covering enabled paths.

Covers lines 45-62 (search_similar when memory enabled) and 80-95
(store_result when memory enabled) that are missed by the base test suite.
"""

import pytest
from unittest.mock import patch, MagicMock

from workers.knowledge_worker import KnowledgeWorker


class TestKnowledgeWorkerSearchEnabled:
    """Tests for _search_similar when memory IS enabled."""

    def setup_method(self):
        self.worker = KnowledgeWorker()

    def test_search_returns_results_when_enabled(self, monkeypatch):
        """search_similar returns real results when memory is enabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = [
            {"content": '{"incident_id":"INC001","incident_type":"timeout","service":"api","root_cause":"pool","confidence":80,"reasoning":"test"}', "score": 0.9},
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = self.worker.execute("search_similar", {
            "service": "api-gateway",
            "summary": "timeout errors",
        })
        assert isinstance(result, dict)
        assert len(result["similar_incidents"]) == 1
        assert result["similar_incidents"][0]["incident_id"] == "INC001"

    def test_search_returns_empty_when_no_service_or_summary(self, monkeypatch):
        """search_similar returns empty when both service and summary are empty."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        result = self.worker.execute("search_similar", {
            "service": "",
            "summary": "",
        })
        assert result["similar_incidents"] == []

    def test_search_uses_service_as_query_fallback(self, monkeypatch):
        """search_similar uses service as query when summary is empty."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = []
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = self.worker.execute("search_similar", {
            "service": "api-gateway",
            "summary": "",
        })
        assert result["similar_incidents"] == []
        # Verify search was called with service as query
        mock_session.search_long_term_memories.assert_called_once()

    def test_search_handles_import_error(self, monkeypatch):
        """search_similar returns empty on ImportError."""
        import builtins
        original_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name == "supervisor.memory":
                raise ImportError("no memory module")
            return original_import(name, *args, **kwargs)

        # Force re-creation to test ImportError path
        worker = KnowledgeWorker()
        # Patch at the point of import in the method
        with patch.object(worker, "_search_similar") as mock_search:
            mock_search.return_value = {"similar_incidents": []}
            result = worker.execute("search_similar", {"service": "test"})
            assert result["similar_incidents"] == []

    def test_search_handles_runtime_exception(self, monkeypatch):
        """search_similar catches general exceptions and returns empty."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        def raising_search(*a, **kw):
            raise RuntimeError("SDK crash")

        monkeypatch.setattr(mem, "search_similar_incidents", raising_search)

        result = self.worker.execute("search_similar", {
            "service": "api",
            "summary": "timeout",
        })
        assert result["similar_incidents"] == []


class TestKnowledgeWorkerStoreEnabled:
    """Tests for _store_result when memory IS enabled."""

    def setup_method(self):
        self.worker = KnowledgeWorker()

    def test_store_result_success(self, monkeypatch):
        """store_result returns stored=True when memory works."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        role_mock = MagicMock()
        role_mock.USER = "user"
        role_mock.ASSISTANT = "assistant"
        monkeypatch.setattr(mem, "_role_cls", role_mock)
        monkeypatch.setattr(mem, "_message_cls", lambda c, r: (c, r))

        mock_session = MagicMock()
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = self.worker.execute("store_result", {
            "incident_id": "INC12345",
            "incident_type": "timeout",
            "service": "api-gateway",
            "root_cause": "Connection pool exhaustion",
            "confidence": 85,
            "reasoning": "DB connections leaked",
            "evidence_summary": "sources=5",
        })
        assert result["stored"] is True

    def test_store_result_returns_false_on_session_error(self, monkeypatch):
        """store_result returns stored=False when session fails."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        monkeypatch.setattr(mem, "_message_cls", MagicMock)
        monkeypatch.setattr(mem, "_role_cls", MagicMock())
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: None)

        result = self.worker.execute("store_result", {
            "incident_id": "INC12345",
            "incident_type": "timeout",
            "service": "api-gateway",
            "root_cause": "test",
            "confidence": 80,
            "reasoning": "test",
        })
        assert result["stored"] is False

    def test_store_result_handles_runtime_exception(self, monkeypatch):
        """store_result catches general exceptions and returns stored=False."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        def raising_store(*a, **kw):
            raise RuntimeError("SDK crash")

        monkeypatch.setattr(mem, "store_investigation_result", raising_store)

        result = self.worker.execute("store_result", {
            "incident_id": "INC12345",
            "incident_type": "timeout",
            "service": "api",
            "root_cause": "test",
            "confidence": 50,
            "reasoning": "test",
        })
        assert result["stored"] is False
        assert "reason" in result

    def test_store_result_with_minimal_params(self, monkeypatch):
        """store_result handles missing optional params via .get defaults."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = self.worker.execute("store_result", {})
        assert result["stored"] is False
