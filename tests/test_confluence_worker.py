"""
Test suite for the Confluence worker.
Validates determinism, correct dispatching, parameter validation, and error handling.
"""

import pytest

from workers.confluence_worker import ConfluenceWorker


class TestConfluenceWorkerInit:
    """ConfluenceWorker instantiation and registration."""

    def test_worker_name(self):
        worker = ConfluenceWorker()
        assert worker.worker_name == "confluence_worker"

    def test_registers_all_actions(self):
        worker = ConfluenceWorker()
        for action in ("search_runbooks", "search_postmortems", "get_page"):
            result = worker.execute(action, {"service": "test-service"})
            assert isinstance(result, dict)


class TestSearchRunbooks:
    """Tests for Confluence runbook search."""

    def setup_method(self):
        self.worker = ConfluenceWorker()

    def test_returns_dict(self):
        result = self.worker.execute("search_runbooks", {"service": "payment-service"})
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"service": "payment-service", "query": "timeout"}
        r1 = self.worker.execute("search_runbooks", params)
        r2 = self.worker.execute("search_runbooks", params)
        assert r1 == r2

    def test_missing_service_returns_error(self):
        result = self.worker.execute("search_runbooks", {})
        assert result.get("error") == "service required"

    def test_empty_service_returns_error(self):
        result = self.worker.execute("search_runbooks", {"service": ""})
        assert result.get("error") == "service required"

    def test_with_optional_query(self):
        result = self.worker.execute(
            "search_runbooks",
            {"service": "api-gateway", "query": "latency"},
        )
        assert isinstance(result, dict)
        assert "error" not in result or result.get("error") != "service required"

    def test_stub_returns_runbooks_key(self):
        result = self.worker.execute("search_runbooks", {"service": "checkout-service"})
        assert "runbooks" in result


class TestSearchPostmortems:
    """Tests for Confluence post-mortem search."""

    def setup_method(self):
        self.worker = ConfluenceWorker()

    def test_returns_dict(self):
        result = self.worker.execute(
            "search_postmortems",
            {"service": "api-gateway", "incident_type": "latency"},
        )
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"service": "api-gateway", "incident_type": "error_spike"}
        r1 = self.worker.execute("search_postmortems", params)
        r2 = self.worker.execute("search_postmortems", params)
        assert r1 == r2

    def test_missing_service_returns_error(self):
        result = self.worker.execute("search_postmortems", {"incident_type": "latency"})
        assert result.get("error") == "service required"

    def test_empty_service_returns_error(self):
        result = self.worker.execute("search_postmortems", {"service": ""})
        assert result.get("error") == "service required"

    def test_without_incident_type(self):
        result = self.worker.execute("search_postmortems", {"service": "payment-service"})
        assert isinstance(result, dict)
        assert "error" not in result or result.get("error") != "service required"

    def test_stub_returns_postmortems_key(self):
        result = self.worker.execute("search_postmortems", {"service": "payment-service"})
        assert "postmortems" in result

    def test_default_time_window(self):
        result = self.worker.execute(
            "search_postmortems",
            {"service": "payment-service"},
        )
        assert isinstance(result, dict)


class TestGetPage:
    """Tests for Confluence page retrieval."""

    def setup_method(self):
        self.worker = ConfluenceWorker()

    def test_returns_dict(self):
        result = self.worker.execute("get_page", {"page_id": "12345"})
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"page_id": "99999"}
        r1 = self.worker.execute("get_page", params)
        r2 = self.worker.execute("get_page", params)
        assert r1 == r2

    def test_missing_page_id_returns_error(self):
        result = self.worker.execute("get_page", {})
        assert result.get("error") == "page_id required"

    def test_empty_page_id_returns_error(self):
        result = self.worker.execute("get_page", {"page_id": ""})
        assert result.get("error") == "page_id required"

    def test_stub_returns_page_key(self):
        result = self.worker.execute("get_page", {"page_id": "42"})
        assert "page" in result


class TestConfluenceWorkerUnknownAction:
    """Unknown action handling."""

    def test_unknown_action_returns_empty(self):
        worker = ConfluenceWorker()
        result = worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)
        assert result == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
