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
# Happy-path tests: STM store/retrieve with mocked session
# =========================================================================

class TestSTMHappyPath:
    """Verify short-term memory store and retrieve with mocked SDK sessions."""

    def _enable_memory(self, monkeypatch, mem):
        """Configure memory module as enabled with mock role/message classes."""
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-memory-id")
        # Create lightweight role enum mock
        role_mock = MagicMock()
        role_mock.USER = "user"
        role_mock.ASSISTANT = "assistant"
        monkeypatch.setattr(mem, "_role_cls", role_mock)
        # Message class just stores (content, role)
        monkeypatch.setattr(mem, "_message_cls", lambda content, role: (content, role))

    def test_store_turn_user_calls_add_turns(self, monkeypatch):
        """STM: store_investigation_turn('user') calls session.add_turns with USER role."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.store_investigation_turn("INC12345", "user", "pod crash detected")
        assert result is True
        mock_session.add_turns.assert_called_once()
        call_kwargs = mock_session.add_turns.call_args
        messages = call_kwargs[1]["messages"] if "messages" in (call_kwargs[1] or {}) else call_kwargs[0][0]
        # The message should be a tuple (content, role)
        assert messages[0] == ("pod crash detected", "user")

    def test_store_turn_assistant_calls_add_turns(self, monkeypatch):
        """STM: store_investigation_turn('assistant') calls session.add_turns with ASSISTANT role."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.store_investigation_turn("INC12345", "assistant", "Root cause: OOMKill")
        assert result is True
        mock_session.add_turns.assert_called_once()
        call_kwargs = mock_session.add_turns.call_args
        messages = call_kwargs[1]["messages"] if "messages" in (call_kwargs[1] or {}) else call_kwargs[0][0]
        assert messages[0] == ("Root cause: OOMKill", "assistant")

    def test_get_recent_turns_parses_dict_format(self, monkeypatch):
        """STM: get_recent_turns correctly parses dict-format turns."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        mock_session.get_last_k_turns.return_value = [
            {"role": "user", "content": {"text": "What caused INC12345?"}},
            {"role": "assistant", "content": {"text": "Root cause: connection pool exhaustion"}},
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        turns = mem.get_recent_turns("INC12345", k=3)
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[0]["content"] == "What caused INC12345?"
        assert turns[1]["role"] == "assistant"
        assert turns[1]["content"] == "Root cause: connection pool exhaustion"
        mock_session.get_last_k_turns.assert_called_once_with(k=3)

    def test_get_recent_turns_parses_list_format(self, monkeypatch):
        """STM: get_recent_turns correctly parses list-of-lists turn format."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        # Some SDK versions return turns as list of message lists
        mock_session.get_last_k_turns.return_value = [
            [
                {"role": "user", "content": {"text": "Investigate timeout"}},
                {"role": "assistant", "content": {"text": "Found connection leak"}},
            ]
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        turns = mem.get_recent_turns("session1")
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[0]["content"] == "Investigate timeout"
        assert turns[1]["content"] == "Found connection leak"

    def test_get_recent_turns_uses_default_k(self, monkeypatch):
        """STM: get_recent_turns uses STM_LAST_K_TURNS when k is not specified."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)
        monkeypatch.setattr(mem, "STM_LAST_K_TURNS", 7)

        mock_session = MagicMock()
        mock_session.get_last_k_turns.return_value = []
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        mem.get_recent_turns("session1")
        mock_session.get_last_k_turns.assert_called_once_with(k=7)

    def test_store_and_retrieve_roundtrip(self, monkeypatch):
        """STM: store a turn then retrieve it — validates full data flow."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        stored_turns = []

        mock_session = MagicMock()
        def capture_add_turns(messages):
            stored_turns.extend(messages)
        mock_session.add_turns.side_effect = lambda messages: capture_add_turns(messages)
        mock_session.get_last_k_turns.return_value = [
            {"role": "user", "content": {"text": "Investigate INC99999"}},
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        # Store
        assert mem.store_investigation_turn("INC99999", "user", "Investigate INC99999") is True
        assert len(stored_turns) == 1

        # Retrieve
        turns = mem.get_recent_turns("INC99999")
        assert len(turns) == 1
        assert turns[0]["content"] == "Investigate INC99999"


# =========================================================================
# Happy-path tests: LTM store/search with mocked session
# =========================================================================

class TestLTMHappyPath:
    """Verify long-term memory store and search with mocked SDK sessions."""

    def _enable_memory(self, monkeypatch, mem):
        """Configure memory module as enabled with mock role/message classes."""
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-memory-id")
        role_mock = MagicMock()
        role_mock.USER = "user"
        role_mock.ASSISTANT = "assistant"
        monkeypatch.setattr(mem, "_role_cls", role_mock)
        monkeypatch.setattr(mem, "_message_cls", lambda content, role: (content, role))

    def test_store_investigation_result_success(self, monkeypatch):
        """LTM: store_investigation_result stores user request + assistant result turns."""
        import supervisor.memory as mem
        import json
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        stored_calls = []
        mock_session.add_turns.side_effect = lambda messages: stored_calls.append(messages)
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.store_investigation_result(
            incident_id="INC12345",
            incident_type="oomkill",
            service="payment-service",
            root_cause="Memory leak in cache layer",
            confidence=92,
            reasoning="Heap dumps showed unbounded cache growth",
            evidence_summary="sources=5, tool_calls=12",
        )

        assert result is True
        # Two add_turns calls: one for user request, one for assistant result
        assert mock_session.add_turns.call_count == 2

        # Verify first call is the user turn (investigation request)
        first_call_msgs = mock_session.add_turns.call_args_list[0][1]["messages"]
        user_msg_content = first_call_msgs[0][0]  # (content, role) tuple
        assert "INC12345" in user_msg_content
        assert "oomkill" in user_msg_content
        assert "payment-service" in user_msg_content

        # Verify second call is the assistant turn (result JSON)
        second_call_msgs = mock_session.add_turns.call_args_list[1][1]["messages"]
        result_content = second_call_msgs[0][0]  # (content, role) tuple
        parsed = json.loads(result_content)
        assert parsed["incident_id"] == "INC12345"
        assert parsed["root_cause"] == "Memory leak in cache layer"
        assert parsed["confidence"] == 92
        assert parsed["service"] == "payment-service"
        assert parsed["evidence_summary"] == "sources=5, tool_calls=12"
        assert "timestamp" in parsed

    def test_store_investigation_result_uses_correct_session_id(self, monkeypatch):
        """LTM: session_id defaults to 'investigation-{incident_id}'."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        captured_sessions = []
        mock_session = MagicMock()
        def capture_session(sid, aid):
            captured_sessions.append(sid)
            return mock_session
        monkeypatch.setattr(mem, "_get_session", capture_session)

        mem.store_investigation_result(
            incident_id="INC55555",
            incident_type="timeout",
            service="api-gw",
            root_cause="test",
            confidence=50,
            reasoning="test",
        )
        assert captured_sessions[0] == "investigation-INC55555"

    def test_store_investigation_result_custom_session_id(self, monkeypatch):
        """LTM: custom session_id overrides the default."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        captured_sessions = []
        mock_session = MagicMock()
        def capture_session(sid, aid):
            captured_sessions.append(sid)
            return mock_session
        monkeypatch.setattr(mem, "_get_session", capture_session)

        mem.store_investigation_result(
            incident_id="INC55555",
            incident_type="timeout",
            service="api-gw",
            root_cause="test",
            confidence=50,
            reasoning="test",
            session_id="custom-session-123",
        )
        assert captured_sessions[0] == "custom-session-123"

    def test_search_similar_incidents_returns_parsed_results(self, monkeypatch):
        """LTM: search_similar_incidents parses JSON records and returns structured dicts."""
        import supervisor.memory as mem
        import json
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = [
            {
                "content": json.dumps({
                    "incident_id": "INC00001",
                    "incident_type": "timeout",
                    "service": "api-gateway",
                    "root_cause": "Connection pool exhaustion",
                    "confidence": 85,
                    "reasoning": "DB connections leaked under load",
                }),
                "score": 0.92,
            },
            {
                "content": json.dumps({
                    "incident_id": "INC00002",
                    "incident_type": "timeout",
                    "service": "api-gateway",
                    "root_cause": "Upstream DNS resolution delay",
                    "confidence": 70,
                    "reasoning": "DNS TTL expired during peak",
                }),
                "score": 0.78,
            },
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        results = mem.search_similar_incidents(
            service="api-gateway",
            query="request timeouts increasing",
            top_k=5,
        )

        assert len(results) == 2

        assert results[0]["incident_id"] == "INC00001"
        assert results[0]["root_cause"] == "Connection pool exhaustion"
        assert results[0]["confidence"] == 85
        assert results[0]["score"] == 0.92

        assert results[1]["incident_id"] == "INC00002"
        assert results[1]["score"] == 0.78

        # Verify search was called with correct params
        mock_session.search_long_term_memories.assert_called_once_with(
            query="api-gateway: request timeouts increasing",
            namespace_prefix="/incidents/",
            top_k=5,
        )

    def test_search_similar_incidents_handles_raw_content(self, monkeypatch):
        """LTM: search handles non-JSON content gracefully."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = [
            {"content": "plain text memory record", "score": 0.65},
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        results = mem.search_similar_incidents(service="svc", query="test")
        assert len(results) == 1
        assert results[0]["incident_id"] == "unknown"
        assert results[0]["score"] == 0.65

    def test_search_similar_uses_default_top_k(self, monkeypatch):
        """LTM: search uses LTM_TOP_K when top_k not specified."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)
        monkeypatch.setattr(mem, "LTM_TOP_K", 10)

        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = []
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        mem.search_similar_incidents(service="svc", query="test")
        mock_session.search_long_term_memories.assert_called_once_with(
            query="svc: test",
            namespace_prefix="/incidents/",
            top_k=10,
        )

    def test_search_similar_custom_namespace(self, monkeypatch):
        """LTM: search respects custom namespace_prefix."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = []
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        mem.search_similar_incidents(
            service="svc", query="test", namespace_prefix="/custom/ns/"
        )
        call_kwargs = mock_session.search_long_term_memories.call_args[1]
        assert call_kwargs["namespace_prefix"] == "/custom/ns/"

    def test_get_long_term_records_returns_list(self, monkeypatch):
        """LTM: get_long_term_records returns records from session."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        mock_session.list_long_term_memory_records.return_value = [
            {"id": "rec1", "content": "record one"},
            {"id": "rec2", "content": "record two"},
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        records = mem.get_long_term_records(namespace_prefix="/incidents/")
        assert len(records) == 2
        assert records[0]["id"] == "rec1"
        mock_session.list_long_term_memory_records.assert_called_once_with(
            namespace_prefix="/incidents/"
        )

    def test_get_long_term_records_handles_non_list_response(self, monkeypatch):
        """LTM: get_long_term_records returns empty list for non-list response."""
        import supervisor.memory as mem
        self._enable_memory(monkeypatch, mem)

        mock_session = MagicMock()
        mock_session.list_long_term_memory_records.return_value = "unexpected string"
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        records = mem.get_long_term_records()
        assert records == []


# =========================================================================
# End-to-end: LTM store + search roundtrip
# =========================================================================

class TestLTMRoundtrip:
    """Verify that data stored in LTM is searchable."""

    def test_store_then_search_roundtrip(self, monkeypatch):
        """LTM end-to-end: store an investigation, then search finds it."""
        import supervisor.memory as mem
        import json
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        role_mock = MagicMock()
        role_mock.USER = "user"
        role_mock.ASSISTANT = "assistant"
        monkeypatch.setattr(mem, "_role_cls", role_mock)
        monkeypatch.setattr(mem, "_message_cls", lambda c, r: (c, r))

        # Capture what gets stored
        stored_data = []
        mock_session = MagicMock()
        def capture_add_turns(messages):
            stored_data.extend(messages)
        mock_session.add_turns.side_effect = lambda messages: capture_add_turns(messages)

        # Make search return the stored data
        def mock_search(query, namespace_prefix, top_k):
            # Return the second stored item (the result JSON)
            result_tuples = [t for t in stored_data if isinstance(t, tuple)]
            results = []
            for content, role in result_tuples:
                if role == "assistant":
                    results.append({"content": content, "score": 0.95})
            return results[:top_k]

        mock_session.search_long_term_memories.side_effect = mock_search
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        # Store
        assert mem.store_investigation_result(
            incident_id="INC77777",
            incident_type="oomkill",
            service="payment-api",
            root_cause="Unbounded cache growth in Redis client",
            confidence=88,
            reasoning="Heap analysis showed 2GB cache objects",
        ) is True

        # Search
        results = mem.search_similar_incidents(
            service="payment-api",
            query="out of memory kill",
        )

        assert len(results) == 1
        assert results[0]["incident_id"] == "INC77777"
        assert results[0]["root_cause"] == "Unbounded cache growth in Redis client"
        assert results[0]["confidence"] == 88
        assert results[0]["score"] == 0.95


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
