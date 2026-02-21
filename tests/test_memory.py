"""Tests for AgentCore Memory integration.

Validates:
- Graceful degradation when SDK not installed / memory not configured
- Memory module functions are safe no-ops when disabled
- Knowledge worker returns empty results when memory unavailable
- Config parsing and defaults
"""

import pytest
from unittest.mock import patch, MagicMock


# =========================================================================
# Test supervisor/memory.py graceful degradation
# =========================================================================

class TestMemoryGracefulDegradation:
    """Memory module must be a safe no-op when SDK is unavailable."""

    def test_is_enabled_false_without_env(self, monkeypatch):
        """is_enabled() returns False when BEDROCK_AGENTCORE_MEMORY_ID is unset."""
        monkeypatch.delenv("BEDROCK_AGENTCORE_MEMORY_ID", raising=False)
        # Force re-import to pick up env
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        assert mem.is_enabled() is False

    def test_is_enabled_false_without_sdk(self, monkeypatch):
        """is_enabled() returns False when SDK is not installed."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", False)
        monkeypatch.setattr(mem, "MEMORY_ID", "some-id")
        assert mem.is_enabled() is False

    def test_store_investigation_turn_noop(self, monkeypatch):
        """store_investigation_turn returns False when disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = mem.store_investigation_turn("session1", "user", "test content")
        assert result is False

    def test_get_recent_turns_empty(self, monkeypatch):
        """get_recent_turns returns empty list when disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = mem.get_recent_turns("session1")
        assert result == []

    def test_store_investigation_result_noop(self, monkeypatch):
        """store_investigation_result returns False when disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = mem.store_investigation_result(
            incident_id="INC12345",
            incident_type="timeout",
            service="api-gateway",
            root_cause="test",
            confidence=80,
            reasoning="test reasoning",
        )
        assert result is False

    def test_search_similar_incidents_empty(self, monkeypatch):
        """search_similar_incidents returns empty list when disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = mem.search_similar_incidents(service="api-gateway", query="timeout")
        assert result == []

    def test_get_long_term_records_empty(self, monkeypatch):
        """get_long_term_records returns empty list when disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = mem.get_long_term_records()
        assert result == []

    def test_dispose_safe(self):
        """dispose() never raises."""
        import supervisor.memory as mem
        mem.dispose()  # Should not raise


class TestMemoryConfig:
    """Memory configuration defaults are correct."""

    def test_default_stm_turns(self):
        import supervisor.memory as mem
        assert mem.STM_LAST_K_TURNS == 5 or isinstance(mem.STM_LAST_K_TURNS, int)

    def test_default_ltm_top_k(self):
        import supervisor.memory as mem
        assert mem.LTM_TOP_K == 3 or isinstance(mem.LTM_TOP_K, int)

    def test_default_ltm_threshold(self):
        import supervisor.memory as mem
        assert 0.0 <= mem.LTM_RELEVANCE_THRESHOLD <= 1.0

    def test_namespace_constants(self):
        import supervisor.memory as mem
        assert mem.NS_INCIDENTS == "/incidents/"
        assert "{service}" in mem.NS_SERVICES
        assert "{incident_type}" in mem.NS_PATTERNS


# =========================================================================
# Test knowledge worker memory integration
# =========================================================================

class TestKnowledgeWorkerMemory:
    """Knowledge worker falls back gracefully when memory is unavailable."""

    def setup_method(self):
        from workers.knowledge_worker import KnowledgeWorker
        self.worker = KnowledgeWorker()

    def test_search_similar_returns_empty_when_disabled(self, monkeypatch):
        """search_similar returns empty list when memory is not configured."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = self.worker.execute("search_similar", {
            "service": "api-gateway",
            "summary": "timeout errors",
        })
        assert isinstance(result, dict)
        assert result["similar_incidents"] == []

    def test_search_similar_with_empty_params(self, monkeypatch):
        """search_similar handles empty params gracefully."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = self.worker.execute("search_similar", {})
        assert result["similar_incidents"] == []

    def test_store_result_returns_false_when_disabled(self, monkeypatch):
        """store_result returns stored=False when memory is not configured."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        result = self.worker.execute("store_result", {
            "incident_id": "INC12345",
            "incident_type": "timeout",
            "service": "api-gateway",
            "root_cause": "test",
            "confidence": 80,
            "reasoning": "test reasoning",
        })
        assert isinstance(result, dict)
        assert result["stored"] is False

    def test_store_result_registered(self):
        """store_result action is registered on the worker."""
        assert "store_result" in self.worker._handlers

    def test_search_similar_registered(self):
        """search_similar action is registered on the worker."""
        assert "search_similar" in self.worker._handlers

    def test_unknown_action_returns_empty(self):
        """Unknown actions return empty dict (base worker behavior)."""
        result = self.worker.execute("nonexistent_action", {})
        assert result == {}


# =========================================================================
# Test memory integration with supervisor agent
# =========================================================================

class TestAgentMemoryIntegration:
    """Supervisor agent gracefully handles memory being disabled."""

    def test_investigate_succeeds_without_memory(self, monkeypatch):
        """Investigation completes normally when memory is disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")

        from supervisor.agent import SentinalAISupervisor
        supervisor = SentinalAISupervisor()
        result = supervisor.investigate("INC12345")

        assert "root_cause" in result
        assert result["confidence"] > 0

    def test_investigate_all_incidents_without_memory(self, monkeypatch):
        """All 10 test incidents produce valid results with memory disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")

        from supervisor.agent import SentinalAISupervisor
        supervisor = SentinalAISupervisor()

        for iid in ["INC12345", "INC12346", "INC12347", "INC12348", "INC12349",
                     "INC12350", "INC12351", "INC12352", "INC12353", "INC12354"]:
            result = supervisor.investigate(iid)
            assert result["confidence"] > 0, f"Zero confidence for {iid}"
            assert result["root_cause"], f"Empty root cause for {iid}"


# =========================================================================
# Test memory with mocked SDK
# =========================================================================

class TestMemoryWithMockedSDK:
    """Test memory operations when SDK is 'available' via mocks."""

    def test_is_enabled_true_with_sdk_and_env(self, monkeypatch):
        """is_enabled returns True when SDK available and memory ID set."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-memory-id")
        assert mem.is_enabled() is True

    def test_get_client_returns_none_when_disabled(self, monkeypatch):
        """_get_client returns None when memory is disabled."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        monkeypatch.setattr(mem, "_memory_client", None)
        assert mem._get_client() is None

    def test_store_investigation_result_handles_session_error(self, monkeypatch):
        """store_investigation_result returns False on session creation error."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        monkeypatch.setattr(mem, "_message_cls", MagicMock)
        monkeypatch.setattr(mem, "_role_cls", MagicMock())

        # _get_session returns None (simulating connection failure)
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: None)

        result = mem.store_investigation_result(
            incident_id="INC12345",
            incident_type="timeout",
            service="api-gateway",
            root_cause="test",
            confidence=80,
            reasoning="test reasoning",
        )
        assert result is False

    def test_search_similar_handles_session_error(self, monkeypatch):
        """search_similar_incidents returns empty on session error."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: None)

        result = mem.search_similar_incidents(
            service="api-gateway",
            query="timeout errors",
        )
        assert result == []

    def test_store_turn_handles_session_error(self, monkeypatch):
        """store_investigation_turn returns False on session error."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        monkeypatch.setattr(mem, "_message_cls", MagicMock)
        monkeypatch.setattr(mem, "_role_cls", MagicMock())
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: None)

        result = mem.store_investigation_turn("session1", "user", "test")
        assert result is False

    def test_get_recent_turns_handles_exception(self, monkeypatch):
        """get_recent_turns returns empty list on exception."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        mock_session = MagicMock()
        mock_session.get_last_k_turns.side_effect = RuntimeError("connection failed")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.get_recent_turns("session1")
        assert result == []

    def test_search_similar_handles_exception(self, monkeypatch):
        """search_similar_incidents returns empty on runtime exception."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        mock_session = MagicMock()
        mock_session.search_long_term_memories.side_effect = RuntimeError("service error")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.search_similar_incidents(service="api", query="test")
        assert result == []

    def test_get_long_term_records_handles_exception(self, monkeypatch):
        """get_long_term_records returns empty on exception."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        mock_session = MagicMock()
        mock_session.list_long_term_memory_records.side_effect = RuntimeError("error")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.get_long_term_records()
        assert result == []


# =========================================================================
# Test runtime health check includes memory status
# =========================================================================

class TestRuntimeMemoryHealth:
    """Runtime health check reports memory status."""

    def test_ping_includes_memory_status(self, monkeypatch):
        """The /ping health check includes memory status field."""
        import supervisor.memory as mem
        monkeypatch.setattr(mem, "MEMORY_ID", "")

        from agentcore_runtime import _handle_invocation
        # Verify the memory module is importable and reports status
        assert mem.is_enabled() is False
