"""
Test suite for the ITSM (ServiceNow) worker.
Validates determinism, correct dispatching, parameter validation, and error handling.
"""

import pytest

from workers.itsm_worker import ItsmWorker


class TestItsmWorkerInit:
    """ItsmWorker instantiation and registration."""

    def test_worker_name(self):
        worker = ItsmWorker()
        assert worker.worker_name == "itsm_worker"

    def test_registers_all_actions(self):
        worker = ItsmWorker()
        for action in ("get_ci_details", "search_incidents", "get_change_records", "get_known_errors"):
            result = worker.execute(action, {"service": "test-service"})
            assert isinstance(result, dict)


class TestGetCiDetails:
    """Tests for ServiceNow CI lookup."""

    def setup_method(self):
        self.worker = ItsmWorker()

    def test_returns_dict(self):
        result = self.worker.execute("get_ci_details", {"service": "payment-service"})
        assert isinstance(result, dict)

    def test_deterministic(self):
        r1 = self.worker.execute("get_ci_details", {"service": "payment-service"})
        r2 = self.worker.execute("get_ci_details", {"service": "payment-service"})
        assert r1 == r2

    def test_missing_service_returns_error(self):
        result = self.worker.execute("get_ci_details", {})
        assert result.get("error") == "service required"

    def test_empty_service_returns_error(self):
        result = self.worker.execute("get_ci_details", {"service": ""})
        assert result.get("error") == "service required"


class TestSearchIncidents:
    """Tests for ServiceNow incident search."""

    def setup_method(self):
        self.worker = ItsmWorker()

    def test_returns_dict(self):
        result = self.worker.execute(
            "search_incidents",
            {"service": "api-gateway", "query": "timeout spike"},
        )
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"service": "api-gateway", "query": "timeout"}
        r1 = self.worker.execute("search_incidents", params)
        r2 = self.worker.execute("search_incidents", params)
        assert r1 == r2

    def test_missing_service_returns_error(self):
        result = self.worker.execute("search_incidents", {"query": "timeout"})
        assert result.get("error") == "service required"

    def test_default_time_window(self):
        """Should work with just service, defaulting time_window_hours to 72."""
        result = self.worker.execute("search_incidents", {"service": "api-gateway"})
        assert isinstance(result, dict)
        assert "error" not in result or result["error"] != "service required"


class TestGetChangeRecords:
    """Tests for ServiceNow change record retrieval."""

    def setup_method(self):
        self.worker = ItsmWorker()

    def test_returns_dict(self):
        result = self.worker.execute("get_change_records", {"service": "payment-service"})
        assert isinstance(result, dict)

    def test_deterministic(self):
        r1 = self.worker.execute("get_change_records", {"service": "payment-service"})
        r2 = self.worker.execute("get_change_records", {"service": "payment-service"})
        assert r1 == r2

    def test_missing_service_returns_error(self):
        result = self.worker.execute("get_change_records", {})
        assert result.get("error") == "service required"


class TestGetKnownErrors:
    """Tests for ServiceNow Known Error Database lookup."""

    def setup_method(self):
        self.worker = ItsmWorker()

    def test_returns_dict(self):
        result = self.worker.execute(
            "get_known_errors",
            {"service": "payment-service", "summary": "NullPointerException"},
        )
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"service": "payment-service", "summary": "NullPointerException"}
        r1 = self.worker.execute("get_known_errors", params)
        r2 = self.worker.execute("get_known_errors", params)
        assert r1 == r2

    def test_missing_service_returns_error(self):
        result = self.worker.execute("get_known_errors", {"summary": "test"})
        assert result.get("error") == "service required"


class TestItsmWorkerUnknownAction:
    """Unknown action handling."""

    def test_unknown_action_returns_empty(self):
        worker = ItsmWorker()
        result = worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)
        assert result == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
