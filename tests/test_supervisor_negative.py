"""
Negative tests, boundary conditions, and regression tests for the supervisor.

These catch real production bugs: malformed data, partial failures,
worker explosions, and regressions for issues fixed during development.
"""

import pytest
from unittest.mock import Mock, MagicMock

from supervisor.agent import SentinalAISupervisor
from tests.fixtures.mock_mcp_responses import ALL_MOCKS
from tests.test_supervisor import _build_mock_workers


# =========================================================================
# Malformed / missing input
# =========================================================================

class TestMalformedInput:
    """Supervisor must never crash on bad input."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()
        # Wire up empty mocks so workers don't crash on real calls
        _build_mock_workers(self.supervisor, "INC_DUMMY")

    def test_empty_incident_id(self):
        result = self.supervisor.investigate("")
        assert "root_cause" in result
        assert "confidence" in result

    def test_none_coerced_incident_id(self):
        """If someone passes 'None' as string, don't crash."""
        result = self.supervisor.investigate("None")
        assert "root_cause" in result

    def test_very_long_incident_id(self):
        result = self.supervisor.investigate("INC" + "9" * 1000)
        assert "root_cause" in result

    def test_special_characters_in_id(self):
        result = self.supervisor.investigate("INC-12345/../../etc/passwd")
        assert "root_cause" in result

    def test_unicode_incident_id(self):
        result = self.supervisor.investigate("INC日本語テスト")
        assert "root_cause" in result


# =========================================================================
# All workers failing
# =========================================================================

class TestAllWorkersFailing:
    """When every worker raises, supervisor must still return valid output."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_all_workers_raise_connectionerror(self):
        for name in self.supervisor.workers:
            mock = MagicMock()
            mock.execute = Mock(side_effect=ConnectionError("service down"))
            self.supervisor.workers[name] = mock

        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result
        assert "confidence" in result
        assert result["confidence"] <= 50

    def test_all_workers_raise_timeout(self):
        for name in self.supervisor.workers:
            mock = MagicMock()
            mock.execute = Mock(side_effect=TimeoutError("timed out"))
            self.supervisor.workers[name] = mock

        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_all_workers_return_none(self):
        for name in self.supervisor.workers:
            mock = MagicMock()
            mock.execute = Mock(return_value=None)
            self.supervisor.workers[name] = mock

        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_all_workers_return_empty_dict(self):
        for name in self.supervisor.workers:
            mock = MagicMock()
            mock.execute = Mock(return_value={})
            self.supervisor.workers[name] = mock

        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result
        assert result["confidence"] <= 50


# =========================================================================
# Individual worker failures
# =========================================================================

class TestPartialWorkerFailure:
    """When specific workers fail, investigation degrades gracefully."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def _fail_worker(self, worker_name, error_cls=ConnectionError, msg="unavailable"):
        mock = MagicMock()
        mock.execute = Mock(side_effect=error_cls(msg))
        self.supervisor.workers[worker_name] = mock

    def test_ops_worker_failure(self):
        """Without ops worker, can't fetch incident. Should return low confidence."""
        _build_mock_workers(self.supervisor, "INC12345")
        self._fail_worker("ops_worker")
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result
        assert result["confidence"] <= 50

    def test_log_worker_failure(self):
        """Without logs, investigation still proceeds with other evidence."""
        _build_mock_workers(self.supervisor, "INC12345")
        self._fail_worker("log_worker")
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_metrics_worker_failure(self):
        _build_mock_workers(self.supervisor, "INC12345")
        self._fail_worker("metrics_worker")
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_apm_worker_failure(self):
        _build_mock_workers(self.supervisor, "INC12345")
        self._fail_worker("apm_worker")
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_knowledge_worker_failure(self):
        _build_mock_workers(self.supervisor, "INC12345")
        self._fail_worker("knowledge_worker")
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result


# =========================================================================
# Worker returning unexpected data shapes
# =========================================================================

class TestUnexpectedWorkerData:
    """Workers may return garbage — supervisor must handle it."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_ops_returns_incident_without_summary(self):
        """Incident missing 'summary' field."""
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.workers["ops_worker"].execute = Mock(
            return_value={"incident": {"incident_id": "INC12345", "affected_service": "api-gateway"}}
        )
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_ops_returns_incident_without_service(self):
        """Incident missing 'affected_service' field."""
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.workers["ops_worker"].execute = Mock(
            return_value={"incident": {"incident_id": "INC12345", "summary": "timeout spike"}}
        )
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_log_worker_returns_string_instead_of_dict(self):
        """Log worker returns a string instead of dict."""
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.workers["log_worker"].execute = Mock(return_value="error")
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result

    def test_metrics_returns_empty_metrics_list(self):
        """Metrics with empty list should not crash."""
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.workers["metrics_worker"].execute = Mock(
            return_value={"metrics": {"metrics": [], "baseline": 0}}
        )
        result = self.supervisor.investigate("INC12345")
        assert "root_cause" in result


# =========================================================================
# Supervisor re-entrancy
# =========================================================================

class TestSupervisorReentrancy:
    """Supervisor must handle sequential investigations cleanly."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_sequential_investigations(self):
        """Investigate multiple incidents in sequence — no state leakage."""
        for incident_id in list(ALL_MOCKS.keys()):
            _build_mock_workers(self.supervisor, incident_id)
            result = self.supervisor.investigate(incident_id)
            assert result["incident_id"] == incident_id

    def test_same_incident_twice(self):
        """Same incident investigated twice should give identical results."""
        _build_mock_workers(self.supervisor, "INC12345")
        r1 = self.supervisor.investigate("INC12345")
        _build_mock_workers(self.supervisor, "INC12345")
        r2 = self.supervisor.investigate("INC12345")
        assert r1["root_cause"] == r2["root_cause"]
        assert r1["confidence"] == r2["confidence"]

    def test_different_incidents_no_leakage(self):
        """Investigating INC12345 then INC12346 should not carry over data."""
        _build_mock_workers(self.supervisor, "INC12345")
        r1 = self.supervisor.investigate("INC12345")
        _build_mock_workers(self.supervisor, "INC12346")
        r2 = self.supervisor.investigate("INC12346")

        assert "timeout" in r1["root_cause"].lower() or "slow" in r1["root_cause"].lower()
        assert "memory" in r2["root_cause"].lower() or "oom" in r2["root_cause"].lower()


# =========================================================================
# Regression tests for specific bugs fixed during development
# =========================================================================

class TestRegressions:
    """Tests for bugs that were found and fixed. They must never recur."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_inc12345_first_timeline_event_is_latency_not_deployment(self):
        """
        Regression: INC12345 had a deployment change at 10:29:00Z that appeared
        before the latency event at 10:30:10Z, making it look like the first
        event was a deployment instead of latency.
        """
        _build_mock_workers(self.supervisor, "INC12345")
        result = self.supervisor.investigate("INC12345")
        timeline = result["evidence_timeline"]
        assert len(timeline) > 0
        first_event = str(timeline[0]).lower()
        assert "latency" in first_event or "spike" in first_event, (
            f"First event should be latency-related, got: {timeline[0]}"
        )

    def test_inc12346_oomkill_evidence_present(self):
        """
        Regression: OOMKill evidence was not appearing in timeline because
        events weren't being explicitly tagged.
        """
        _build_mock_workers(self.supervisor, "INC12346")
        result = self.supervisor.investigate("INC12346")
        evidence_text = str(result["evidence_timeline"]).lower()
        assert "oomkill" in evidence_text

    def test_inc12352_classified_as_missing_data(self):
        """
        Regression: 'notification-service degraded' was classified as error_spike
        because 'degraded' wasn't in missing_data keywords.
        """
        from supervisor.tool_selector import classify_incident
        result = classify_incident("notification-service degraded")
        assert result == "missing_data"

    def test_inc12354_pipeline_logs_found(self):
        """
        Regression: Pipeline logs were not returned because 'recommendation'
        keyword matched before 'pipeline' in mock iteration order.
        """
        _build_mock_workers(self.supervisor, "INC12354")
        result = self.supervisor.investigate("INC12354")
        assert "pipeline" in result["root_cause"].lower()
        assert "stale" in result["root_cause"].lower()

    def test_inc12351_cascading_mentions_payment_service(self):
        """
        Regression: Cascading failure had hardcoded 'payment-service' in the
        root cause. Verify it's still present (since the mock data IS about
        payment-service).
        """
        _build_mock_workers(self.supervisor, "INC12351")
        result = self.supervisor.investigate("INC12351")
        assert "payment-service" in result["root_cause"].lower()
        assert "connection pool" in result["root_cause"].lower()


# =========================================================================
# Playbook coverage - verify correct workers are called
# =========================================================================

class TestPlaybookCoverage:
    """Each incident type must call the expected workers."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def _get_call_log(self, incident_id):
        """Investigate and return which workers were called."""
        _build_mock_workers(self.supervisor, incident_id)
        self.supervisor.investigate(incident_id)
        calls = {}
        for name in ("ops_worker", "log_worker", "metrics_worker", "apm_worker"):
            mock = self.supervisor.workers[name]
            calls[name] = mock.execute.call_count
        return calls

    def test_timeout_calls_ops_logs_apm_metrics(self):
        calls = self._get_call_log("INC12345")
        assert calls["ops_worker"] >= 1
        assert calls["log_worker"] >= 1
        assert calls["apm_worker"] >= 1
        assert calls["metrics_worker"] >= 1

    def test_oomkill_calls_ops_logs_metrics(self):
        calls = self._get_call_log("INC12346")
        assert calls["ops_worker"] >= 1
        assert calls["log_worker"] >= 1
        assert calls["metrics_worker"] >= 1

    def test_error_spike_calls_ops_logs_apm(self):
        calls = self._get_call_log("INC12347")
        assert calls["ops_worker"] >= 1
        assert calls["log_worker"] >= 1
        assert calls["apm_worker"] >= 1

    def test_network_calls_ops_logs_apm(self):
        calls = self._get_call_log("INC12350")
        assert calls["ops_worker"] >= 1
        assert calls["log_worker"] >= 1
        assert calls["apm_worker"] >= 1

    def test_cascading_calls_ops_logs_apm_metrics(self):
        calls = self._get_call_log("INC12351")
        assert calls["ops_worker"] >= 1
        assert calls["log_worker"] >= 1
        assert calls["apm_worker"] >= 1
        assert calls["metrics_worker"] >= 1

    def test_flapping_calls_ops_logs_apm_metrics(self):
        calls = self._get_call_log("INC12353")
        assert calls["ops_worker"] >= 1
        assert calls["log_worker"] >= 1
        assert calls["apm_worker"] >= 1
        assert calls["metrics_worker"] >= 1


# =========================================================================
# Worker call arguments validation
# =========================================================================

class TestWorkerCallArguments:
    """Verify the supervisor passes correct parameters to workers."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_ops_called_with_incident_id(self):
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.investigate("INC12345")
        ops_calls = self.supervisor.workers["ops_worker"].execute.call_args_list
        # First call should be get_incident_by_id with the incident_id
        first_call = ops_calls[0]
        assert first_call[0][0] == "get_incident_by_id"
        assert first_call[0][1]["incident_id"] == "INC12345"

    def test_log_search_includes_service_name(self):
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.investigate("INC12345")
        log_calls = self.supervisor.workers["log_worker"].execute.call_args_list
        # At least one call should include service in query
        queries = [
            call[0][1].get("query", "")
            for call in log_calls
            if call[0][0] == "search_logs"
        ]
        assert any("api-gateway" in q for q in queries), (
            f"Log search should include service name. Queries: {queries}"
        )

    def test_apm_called_with_service(self):
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.investigate("INC12345")
        apm_calls = self.supervisor.workers["apm_worker"].execute.call_args_list
        for call in apm_calls:
            if call[0][0] == "get_golden_signals":
                params = call[0][1]
                assert "service" in params or "target" in params


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
