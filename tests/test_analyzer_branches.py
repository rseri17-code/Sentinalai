"""
Tests targeting uncovered branches in supervisor/agent.py.

Exercises fallback paths in each type-specific analyzer when evidence
is incomplete, absent, or doesn't match the primary detection pattern.
"""

import pytest
from unittest.mock import Mock, MagicMock

from supervisor.agent import SentinalAISupervisor


def _make_supervisor_with_data(incident_data, log_data=None, signals_data=None,
                                metrics_data=None, events_data=None, changes_data=None):
    """Create a supervisor with precisely controlled mock data."""
    supervisor = SentinalAISupervisor()

    def mock_ops(action, params):
        if action == "get_incident_by_id":
            return {"incident": incident_data}
        return {}

    def mock_logs(action, params):
        if action == "search_logs" and log_data is not None:
            return {"logs": log_data}
        if action == "get_change_data" and changes_data is not None:
            return changes_data
        return {}

    def mock_metrics(action, params):
        if action in ("query_metrics", "get_resource_metrics") and metrics_data is not None:
            return {"metrics": metrics_data}
        if action == "get_events" and events_data is not None:
            return events_data
        return {}

    def mock_apm(action, params):
        if action in ("get_golden_signals", "check_latency") and signals_data is not None:
            return {"signals": signals_data}
        return {}

    def mock_knowledge(action, params):
        return {"similar_incidents": []}

    for name in supervisor.workers:
        supervisor.workers[name] = MagicMock()

    supervisor.workers["ops_worker"].execute = Mock(side_effect=mock_ops)
    supervisor.workers["log_worker"].execute = Mock(side_effect=mock_logs)
    supervisor.workers["metrics_worker"].execute = Mock(side_effect=mock_metrics)
    supervisor.workers["apm_worker"].execute = Mock(side_effect=mock_apm)
    supervisor.workers["knowledge_worker"].execute = Mock(side_effect=mock_knowledge)

    return supervisor


# =========================================================================
# Timeout analyzer fallback (no downstream service found)
# =========================================================================

class TestTimeoutFallback:
    def test_timeout_without_downstream_service(self):
        """When logs don't identify a downstream service, fallback path."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_T1",
                "summary": "API Gateway timeout spike",
                "affected_service": "api-gateway",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "Request failed with timeout", "service": "api-gateway"},
                ],
            },
            signals_data={
                "golden_signals": {"latency": {"p95": 100, "baseline_p95": 50}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "latency_spike",
            },
        )
        result = supervisor.investigate("INC_T1")
        assert "root_cause" in result
        assert result["confidence"] <= 55  # Lower confidence without clear downstream


# =========================================================================
# Error spike analyzer branches
# =========================================================================

class TestErrorSpikeBranches:
    def test_error_type_found_but_no_deployment(self):
        """NullPointerException found but no change data -> lower confidence."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_E1",
                "summary": "Payment service error spike",
                "affected_service": "payment-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "NullPointerException in handler",
                     "service": "payment-service", "exception": "NullPointerException"},
                ],
            },
            changes_data={"changes": []},
            signals_data={
                "golden_signals": {"errors": {"rate": 0.35}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "error_spike",
            },
        )
        result = supervisor.investigate("INC_E1")
        assert "NullPointerException" in result["root_cause"]
        assert 50 <= result["confidence"] <= 70

    def test_no_error_type_no_deployment(self):
        """No specific error type identified -> lowest confidence."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_E2",
                "summary": "Payment service error spike",
                "affected_service": "payment-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "something went wrong", "service": "payment-service"},
                ],
            },
            changes_data={"changes": []},
            signals_data={
                "golden_signals": {"errors": {"rate": 0.35}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "error_spike",
            },
        )
        result = supervisor.investigate("INC_E2")
        assert 40 <= result["confidence"] <= 60


# =========================================================================
# Latency analyzer fallback (no backend identified)
# =========================================================================

class TestLatencyFallback:
    def test_latency_no_backend(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_L1",
                "summary": "search-service slow response time",
                "affected_service": "search-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "WARN",
                     "message": "Slow response detected", "service": "search-service"},
                ],
            },
            signals_data={
                "golden_signals": {"latency": {"p95": 5000, "baseline_p95": 100}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "latency_spike",
            },
        )
        result = supervisor.investigate("INC_L1")
        assert "latency" in result["root_cause"].lower()
        assert 45 <= result["confidence"] <= 65


# =========================================================================
# Saturation analyzer branches
# =========================================================================

class TestSaturationBranches:
    def test_high_cpu_no_deployment(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_S1",
                "summary": "order-service CPU exhaustion",
                "affected_service": "order-service",
            },
            signals_data={
                "golden_signals": {"saturation": {"cpu": 98}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "saturation",
            },
            changes_data={"changes": []},
        )
        result = supervisor.investigate("INC_S1")
        assert "cpu" in result["root_cause"].lower()
        assert 55 <= result["confidence"] <= 75

    def test_low_cpu_saturation(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_S2",
                "summary": "order-service CPU exhaustion",
                "affected_service": "order-service",
            },
            signals_data={
                "golden_signals": {"saturation": {"cpu": 50}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "saturation",
            },
            changes_data={"changes": []},
        )
        result = supervisor.investigate("INC_S2")
        assert "saturation" in result["root_cause"].lower()
        assert 35 <= result["confidence"] <= 60


# =========================================================================
# Network analyzer branches
# =========================================================================

class TestNetworkBranches:
    def test_dns_issue_no_deployment(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_N1",
                "summary": "connectivity failure across cluster",
                "affected_service": "inventory-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "dns resolution failed for service-a", "service": "inv"},
                ],
            },
            changes_data={"changes": []},
        )
        result = supervisor.investigate("INC_N1")
        assert "dns" in result["root_cause"].lower()
        assert 55 <= result["confidence"] <= 80

    def test_no_dns_no_deployment(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_N2",
                "summary": "connectivity failure across cluster",
                "affected_service": "inventory-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "connection refused to service-b", "service": "inv"},
                ],
            },
            changes_data={"changes": []},
        )
        result = supervisor.investigate("INC_N2")
        assert "network" in result["root_cause"].lower()
        assert 30 <= result["confidence"] <= 60


# =========================================================================
# Cascading analyzer fallback
# =========================================================================

class TestCascadingFallback:
    def test_cascading_no_pool_exhaustion_no_deployment(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_C1",
                "summary": "Cascading failure in services",
                "affected_service": "api-gateway",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "Upstream service failed", "service": "api-gateway"},
                ],
            },
            changes_data={"changes": []},
        )
        result = supervisor.investigate("INC_C1")
        assert "cascading" in result["root_cause"].lower()
        assert 35 <= result["confidence"] <= 65


# =========================================================================
# Missing data analyzer fallback
# =========================================================================

class TestMissingDataFallback:
    def test_no_connection_error_found(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_M1",
                "summary": "notification-service degraded",
                "affected_service": "notification-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "WARN",
                     "message": "performance degradation detected",
                     "service": "notification-service"},
                ],
            },
        )
        result = supervisor.investigate("INC_M1")
        assert result["confidence"] <= 40


# =========================================================================
# Flapping analyzer fallback
# =========================================================================

class TestFlappingFallback:
    def test_no_sawtooth_no_intermittent(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_F1",
                "summary": "auth-service intermittent failures",
                "affected_service": "auth-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "Authentication failed", "service": "auth-service"},
                ],
            },
            signals_data={
                "golden_signals": {},
                "anomaly_type": "error_spike",
            },
            metrics_data={"metrics": [], "pattern": "flat"},
        )
        result = supervisor.investigate("INC_F1")
        assert "intermittent" in result["root_cause"].lower()
        assert 25 <= result["confidence"] <= 55


# =========================================================================
# Silent failure analyzer branches
# =========================================================================

class TestSilentFailureBranches:
    def test_pipeline_failure_no_stale_cache(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_SF1",
                "summary": "recommendation-service throughput drop",
                "affected_service": "recommendation-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "Data pipeline job failed with error",
                     "service": "data-pipeline"},
                ],
            },
            signals_data={
                "golden_signals": {},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "throughput_drop",
            },
        )
        result = supervisor.investigate("INC_SF1")
        assert "pipeline" in result["root_cause"].lower()
        assert 50 <= result["confidence"] <= 75

    def test_no_pipeline_no_stale_cache(self):
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_SF2",
                "summary": "recommendation-service throughput drop",
                "affected_service": "recommendation-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "INFO",
                     "message": "Normal operation log",
                     "service": "recommendation-service"},
                ],
            },
            signals_data={
                "golden_signals": {},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "throughput_drop",
            },
        )
        result = supervisor.investigate("INC_SF2")
        assert "throughput" in result["root_cause"].lower()
        assert 25 <= result["confidence"] <= 55


# =========================================================================
# Generic analyzer (unrecognized type)
# =========================================================================

class TestGenericAnalyzer:
    def test_unknown_incident_type_uses_generic(self):
        """If classification returns unknown type, generic analyzer runs."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_G1",
                "summary": "Something completely unknown",
                "affected_service": "mystery-service",
            },
        )
        result = supervisor.investigate("INC_G1")
        assert result["confidence"] <= 60


# =========================================================================
# Anomaly description branch (generic fallback)
# =========================================================================

class TestAnomalyDescription:
    def test_unknown_anomaly_type_description(self):
        """When anomaly_type is unrecognized, fallback description is used."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_AD1",
                "summary": "api-gateway timeout spike",
                "affected_service": "api-gateway",
            },
            signals_data={
                "golden_signals": {"latency": {}, "errors": {}, "saturation": {}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "completely_unknown_type",
            },
        )
        result = supervisor.investigate("INC_AD1")
        evidence_text = str(result["evidence_timeline"]).lower()
        assert "anomaly" in evidence_text or "unknown" in evidence_text


# =========================================================================
# Helper method edge cases
# =========================================================================

class TestHelperMethods:
    """Test internal helper methods through investigation output."""

    def test_find_connection_target_postgres(self):
        """Logs mentioning postgres should identify it as target."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H1",
                "summary": "notification-service degraded",
                "affected_service": "notification-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "postgres connection refused: ECONNREFUSED",
                     "service": "notification-service",
                     "error_type": "connection_refused"},
                ],
            },
        )
        result = supervisor.investigate("INC_H1")
        # Should identify postgres as the connection target
        assert "database" in result["root_cause"].lower() or "postgres" in result["root_cause"].lower()

    def test_find_backend_redis(self):
        """Logs mentioning redis should identify it as backend."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H2",
                "summary": "search-service slow response time",
                "affected_service": "search-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "WARN",
                     "message": "Redis response timeout 5000ms",
                     "service": "search-service"},
                ],
            },
            signals_data={
                "golden_signals": {"latency": {"p95": 5000, "baseline_p95": 100}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "latency_spike",
            },
        )
        result = supervisor.investigate("INC_H2")
        assert "redis" in result["root_cause"].lower()

    def test_find_backend_database(self):
        """Logs mentioning 'database' should identify it as backend."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H3",
                "summary": "search-service slow response time",
                "affected_service": "search-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "WARN",
                     "message": "database query took 12s",
                     "service": "search-service"},
                ],
            },
            signals_data={
                "golden_signals": {"latency": {"p95": 5000, "baseline_p95": 100}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "latency_spike",
            },
        )
        result = supervisor.investigate("INC_H3")
        assert "database" in result["root_cause"].lower()

    def test_is_gradual_increase_with_flat_data(self):
        """Non-increasing metrics should not be classified as gradual increase."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H4",
                "summary": "user-service OOMKilled",
                "affected_service": "user-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "OOMKilled: user-service", "service": "user-service"},
                ],
            },
            metrics_data={
                "metrics": [
                    {"timestamp": "2024-01-01T09:00:00Z", "name": "memory", "value": 4000000000},
                    {"timestamp": "2024-01-01T09:30:00Z", "name": "memory", "value": 4000000000},
                    {"timestamp": "2024-01-01T10:00:00Z", "name": "memory", "value": 4000000000},
                ],
                "pattern": "",
                "limit": 8000000000,
            },
            events_data={
                "events": [
                    {"type": "pod_restart", "severity": "high",
                     "message": "Pod OOMKilled",
                     "timestamp": "2024-01-01T10:00:00Z"},
                ],
            },
        )
        result = supervisor.investigate("INC_H4")
        # Should still identify OOMKill but with lower confidence (no gradual pattern)
        assert "oom" in result["root_cause"].lower() or "memory" in result["root_cause"].lower()
        assert 55 <= result["confidence"] <= 75

    def test_detect_sawtooth_from_metrics(self):
        """Sawtooth pattern detected from raw metric values (not pattern field)."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H5",
                "summary": "auth-service intermittent failures",
                "affected_service": "auth-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "Authentication failed: connection pool exhausted",
                     "service": "auth-service"},
                ],
            },
            signals_data={
                "golden_signals": {},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "intermittent_errors",
            },
            # Sawtooth from values, not from pattern field
            metrics_data={
                "metrics": [
                    {"timestamp": "2024-01-01T10:00:00Z", "name": "pool", "value": 50},
                    {"timestamp": "2024-01-01T10:02:00Z", "name": "pool", "value": 20},
                    {"timestamp": "2024-01-01T10:05:00Z", "name": "pool", "value": 50},
                    {"timestamp": "2024-01-01T10:07:00Z", "name": "pool", "value": 15},
                    {"timestamp": "2024-01-01T10:12:00Z", "name": "pool", "value": 50},
                ],
                "pool_max": 50,
            },
        )
        result = supervisor.investigate("INC_H5")
        assert "connection pool" in result["root_cause"].lower()

    def test_connection_error_timeout_type(self):
        """Connection error via timeout keyword in logs."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H6",
                "summary": "notification-service degraded",
                "affected_service": "notification-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "connection timeout to backend",
                     "service": "notification-service"},
                ],
            },
        )
        result = supervisor.investigate("INC_H6")
        assert result["confidence"] >= 50

    def test_downstream_from_downstream_field(self):
        """Downstream service identified from log's 'downstream' field."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H7",
                "summary": "API Gateway timeout spike",
                "affected_service": "api-gateway",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "upstream request timeout",
                     "service": "api-gateway",
                     "downstream": "payment-service"},
                ],
            },
            signals_data={
                "golden_signals": {"latency": {"p95": 30000, "baseline_p95": 200}},
                "anomaly_start": "2024-01-01T10:00:00Z",
                "anomaly_type": "latency_spike",
            },
        )
        result = supervisor.investigate("INC_H7")
        assert "payment-service" in result["root_cause"].lower()

    def test_resolve_hostname_dns_detection(self):
        """'resolve hostname' keyword triggers DNS detection."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H8",
                "summary": "connectivity failure",
                "affected_service": "inventory-service",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "unable to resolve hostname catalog-service.local",
                     "service": "inventory-service"},
                ],
            },
            changes_data={"changes": []},
        )
        result = supervisor.investigate("INC_H8")
        assert "dns" in result["root_cause"].lower()

    def test_pool_exhaustion_from_connection_pool(self):
        """'connection pool' keyword triggers pool exhaustion detection."""
        supervisor = _make_supervisor_with_data(
            incident_data={
                "incident_id": "INC_H9",
                "summary": "Cascading failure in services",
                "affected_service": "api-gateway",
            },
            log_data={
                "results": [
                    {"_time": "2024-01-01T10:00:00Z", "level": "ERROR",
                     "message": "connection pool saturated, all connections in use",
                     "service": "payment-service"},
                ],
            },
            changes_data={
                "changes": [
                    {"change_type": "database_migration", "service": "payment-db",
                     "description": "Drop index", "scheduled_start": "2024-01-01T09:55:00Z"},
                ],
            },
        )
        result = supervisor.investigate("INC_H9")
        assert result["confidence"] >= 70

    def test_missing_worker_in_playbook(self):
        """If a worker is missing from the dict, playbook continues."""
        supervisor = SentinalAISupervisor()
        # Remove a worker entirely
        del supervisor.workers["metrics_worker"]

        # Wire up remaining workers
        def mock_ops(action, params):
            return {"incident": {
                "incident_id": "INC_MW", "summary": "test timeout",
                "affected_service": "svc",
            }}
        supervisor.workers["ops_worker"] = MagicMock()
        supervisor.workers["ops_worker"].execute = Mock(side_effect=mock_ops)
        for name in ("log_worker", "apm_worker", "knowledge_worker"):
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(return_value={})

        result = supervisor.investigate("INC_MW")
        assert "root_cause" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
