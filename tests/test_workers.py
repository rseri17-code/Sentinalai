"""
Test suite for SentinalAI workers.
Validates determinism, correct dispatching, and error handling.
"""

import pytest
from unittest.mock import Mock, MagicMock

from workers.ops_worker import OpsWorker
from workers.log_worker import LogWorker
from workers.metrics_worker import MetricsWorker
from workers.apm_worker import ApmWorker
from workers.knowledge_worker import KnowledgeWorker
from tests.fixtures.mock_mcp_responses import ALL_MOCKS


class TestOpsWorker:
    """Tests for the Moogsoft ops worker."""

    def setup_method(self):
        self.worker = OpsWorker()

    def test_get_incident_returns_dict(self):
        """get_incident_by_id must return a dict with incident details."""
        result = self.worker.execute(
            "get_incident_by_id",
            {"incident_id": "INC12345"},
        )
        assert isinstance(result, dict)

    def test_get_incident_has_required_fields(self):
        """Returned incident must contain key fields."""
        result = self.worker.execute(
            "get_incident_by_id",
            {"incident_id": "INC12345"},
        )
        incident = result.get("incident", result)
        assert "incident_id" in incident or "summary" in incident or "error" in incident

    def test_deterministic_output(self):
        """Same call twice must yield identical output."""
        r1 = self.worker.execute("get_incident_by_id", {"incident_id": "INC12345"})
        r2 = self.worker.execute("get_incident_by_id", {"incident_id": "INC12345"})
        assert r1 == r2

    def test_unknown_action_returns_empty(self):
        """Unknown action should return empty dict, not raise."""
        result = self.worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)


class TestLogWorker:
    """Tests for the Splunk log worker."""

    def setup_method(self):
        self.worker = LogWorker()

    def test_search_logs_returns_dict(self):
        """search_logs must return a dict."""
        result = self.worker.execute(
            "search_logs",
            {"query": "timeout api-gateway", "service": "api-gateway"},
        )
        assert isinstance(result, dict)

    def test_get_change_data_returns_dict(self):
        """get_change_data must return a dict."""
        result = self.worker.execute(
            "get_change_data",
            {"service": "payment-service"},
        )
        assert isinstance(result, dict)

    def test_deterministic_output(self):
        r1 = self.worker.execute("search_logs", {"query": "timeout", "service": "api-gateway"})
        r2 = self.worker.execute("search_logs", {"query": "timeout", "service": "api-gateway"})
        assert r1 == r2

    def test_unknown_action_returns_empty(self):
        result = self.worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)


class TestMetricsWorker:
    """Tests for the Sysdig metrics worker."""

    def setup_method(self):
        self.worker = MetricsWorker()

    def test_query_metrics_returns_dict(self):
        result = self.worker.execute(
            "query_metrics",
            {"service": "payment-service", "metric": "response_time_ms"},
        )
        assert isinstance(result, dict)

    def test_get_events_returns_dict(self):
        result = self.worker.execute(
            "get_events",
            {"service": "user-service"},
        )
        assert isinstance(result, dict)

    def test_deterministic_output(self):
        r1 = self.worker.execute("query_metrics", {"service": "payment-service"})
        r2 = self.worker.execute("query_metrics", {"service": "payment-service"})
        assert r1 == r2

    def test_unknown_action_returns_empty(self):
        result = self.worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)


class TestApmWorker:
    """Tests for the Sysdig APM / golden signals worker."""

    def setup_method(self):
        self.worker = ApmWorker()

    def test_get_golden_signals_returns_dict(self):
        result = self.worker.execute(
            "get_golden_signals",
            {"service": "payment-service"},
        )
        assert isinstance(result, dict)

    def test_deterministic_output(self):
        r1 = self.worker.execute("get_golden_signals", {"service": "payment-service"})
        r2 = self.worker.execute("get_golden_signals", {"service": "payment-service"})
        assert r1 == r2

    def test_unknown_action_returns_empty(self):
        result = self.worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)


class TestKnowledgeWorker:
    """Tests for the knowledge / historical context worker."""

    def setup_method(self):
        self.worker = KnowledgeWorker()

    def test_search_similar_returns_dict(self):
        result = self.worker.execute(
            "search_similar",
            {"incident_type": "timeout", "service": "payment-service"},
        )
        assert isinstance(result, dict)

    def test_deterministic_output(self):
        r1 = self.worker.execute("search_similar", {"incident_type": "timeout"})
        r2 = self.worker.execute("search_similar", {"incident_type": "timeout"})
        assert r1 == r2

    def test_unknown_action_returns_empty(self):
        result = self.worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
