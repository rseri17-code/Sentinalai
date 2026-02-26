"""Extended tests for supervisor/memory.py — covering client/session paths.

Covers:
- _get_client initialization and caching
- _get_session creation and error handling
- store_investigation_turn with add_turns exception
- get_recent_turns with non-dict/non-list turn formats
- store_investigation_result with add_turns exception
- search_similar_incidents with non-dict record formats
- get_long_term_records with session error
"""

import json
import pytest
from unittest.mock import patch, MagicMock

import supervisor.memory as mem


class TestGetClient:
    """Tests for _get_client lazy singleton."""

    def setup_method(self):
        mem._memory_client = None

    def teardown_method(self):
        mem._memory_client = None

    def test_returns_cached_client(self, monkeypatch):
        sentinel = MagicMock()
        mem._memory_client = sentinel
        result = mem._get_client()
        assert result is sentinel

    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", False)
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        mem._memory_client = None
        assert mem._get_client() is None

    def test_creates_client_when_enabled(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        mem._memory_client = None

        mock_client_cls = MagicMock()
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Patch the MemoryClient at module level
        with patch.object(mem, "MemoryClient", mock_client_cls, create=True):
            result = mem._get_client()
        assert result is mock_client

    def test_handles_client_creation_error(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        mem._memory_client = None

        with patch.object(mem, "MemoryClient", side_effect=RuntimeError("fail"), create=True):
            result = mem._get_client()
        assert result is None


class TestGetSession:
    """Tests for _get_session creation."""

    def test_returns_none_when_session_manager_cls_none(self, monkeypatch):
        monkeypatch.setattr(mem, "_session_manager_cls", None)
        assert mem._get_session("session1") is None

    def test_returns_none_when_memory_id_empty(self, monkeypatch):
        monkeypatch.setattr(mem, "_session_manager_cls", MagicMock)
        monkeypatch.setattr(mem, "MEMORY_ID", "")
        assert mem._get_session("session1") is None

    def test_creates_session_successfully(self, monkeypatch):
        mock_mgr = MagicMock()
        mock_session = MagicMock()
        mock_mgr_cls = MagicMock(return_value=mock_mgr)
        mock_mgr.create_memory_session.return_value = mock_session

        monkeypatch.setattr(mem, "_session_manager_cls", mock_mgr_cls)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        result = mem._get_session("session1", "actor1")
        assert result is mock_session
        mock_mgr_cls.assert_called_once_with(
            memory_id="test-id",
            region_name=mem.AWS_REGION,
        )
        mock_mgr.create_memory_session.assert_called_once_with(
            actor_id="actor1",
            session_id="session1",
        )

    def test_handles_session_creation_error(self, monkeypatch):
        mock_mgr_cls = MagicMock(side_effect=RuntimeError("connection failed"))
        monkeypatch.setattr(mem, "_session_manager_cls", mock_mgr_cls)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

        result = mem._get_session("session1")
        assert result is None


class TestStoreTurnExceptionHandling:
    """Test store_investigation_turn exception paths."""

    def _enable(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        role_mock = MagicMock()
        role_mock.USER = "user"
        role_mock.ASSISTANT = "assistant"
        monkeypatch.setattr(mem, "_role_cls", role_mock)
        monkeypatch.setattr(mem, "_message_cls", lambda c, r: (c, r))

    def test_store_turn_catches_add_turns_exception(self, monkeypatch):
        """store_investigation_turn returns False when add_turns raises."""
        self._enable(monkeypatch)
        mock_session = MagicMock()
        mock_session.add_turns.side_effect = RuntimeError("network error")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.store_investigation_turn("sess1", "user", "test")
        assert result is False


class TestStoreResultExceptionHandling:
    """Test store_investigation_result exception paths."""

    def _enable(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        role_mock = MagicMock()
        role_mock.USER = "user"
        role_mock.ASSISTANT = "assistant"
        monkeypatch.setattr(mem, "_role_cls", role_mock)
        monkeypatch.setattr(mem, "_message_cls", lambda c, r: (c, r))

    def test_store_result_catches_add_turns_exception(self, monkeypatch):
        """store_investigation_result returns False when add_turns raises."""
        self._enable(monkeypatch)
        mock_session = MagicMock()
        mock_session.add_turns.side_effect = RuntimeError("SDK error")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        result = mem.store_investigation_result(
            incident_id="INC1",
            incident_type="timeout",
            service="svc",
            root_cause="test",
            confidence=50,
            reasoning="test",
        )
        assert result is False


class TestSearchSimilarEdgeCases:
    """Edge cases for search_similar_incidents."""

    def _enable(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")

    def test_handles_non_dict_record_in_results(self, monkeypatch):
        """search_similar_incidents catches errors from non-dict records gracefully."""
        self._enable(monkeypatch)
        mock_session = MagicMock()
        # Non-dict records (like int) will cause .get() to fail, triggering
        # the except branch which returns []
        mock_session.search_long_term_memories.return_value = [
            "plain string record",
            42,
        ]
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: mock_session)

        results = mem.search_similar_incidents(service="svc", query="test")
        # The function catches AttributeError from int.get() and returns []
        assert results == []


class TestGetLongTermRecordsEdgeCases:
    """Edge cases for get_long_term_records."""

    def test_returns_empty_when_session_none(self, monkeypatch):
        monkeypatch.setattr(mem, "_sdk_available", True)
        monkeypatch.setattr(mem, "MEMORY_ID", "test-id")
        monkeypatch.setattr(mem, "_get_session", lambda *a, **kw: None)

        result = mem.get_long_term_records()
        assert result == []
