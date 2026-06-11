"""Tests for ThousandEyes network worker and integration components.

Covers:
- Feature flag gate (ENABLE_THOUSANDEYES_RCA=false → no-op)
- Fixture mode (TE_USE_FIXTURES=true → offline)
- Normalizer: NetworkEvidence + confidence scoring + owner inference
- Correlation rules: TE-CORR-001 through TE-CORR-006
- Worker actions: get_network_evidence, get_network_alerts, check_network_health
- No regression: existing workers unaffected when flag is off
"""

from __future__ import annotations

import pytest

from workers.network_worker import ThousandEyesWorker
from integrations.thousandeyes.normalizer import (
    NetworkEvidence,
    compute_network_confidence,
    infer_owner,
    normalize_alert,
    normalize_test_result,
    aggregate_scope,
)
from integrations.thousandeyes.correlation import (
    run_all_rules,
    rule_001_network_induced_latency,
    rule_004_dns_root_cause,
    rule_006_saas_provider_outage,
)
from integrations.thousandeyes import fixture_loader


# ---------------------------------------------------------------------------
# Feature flag gate
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    def test_disabled_by_default(self, monkeypatch):
        """When flag is off (default), all actions return empty dict."""
        monkeypatch.delenv("ENABLE_THOUSANDEYES_RCA", raising=False)
        worker = ThousandEyesWorker()
        assert worker.execute("get_network_evidence", {}) == {}
        assert worker.execute("get_network_alerts", {}) == {}

    def test_check_health_flag_off(self, monkeypatch):
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
        worker = ThousandEyesWorker()
        result = worker.execute("check_network_health", {})
        assert result["enabled"] is False

    def test_check_health_flag_on_fixture_mode(self, monkeypatch):
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "true")
        monkeypatch.setenv("TE_USE_FIXTURES", "true")
        worker = ThousandEyesWorker()
        result = worker.execute("check_network_health", {})
        assert result["enabled"] is True
        assert result["healthy"] is True
        assert result["test_count"] >= 0

    def test_unknown_action_returns_empty(self):
        worker = ThousandEyesWorker()
        assert worker.execute("nonexistent_action", {}) == {}


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------

class TestFixtureLoader:
    def test_load_list_tests(self):
        data = fixture_loader.load("te_list_tests")
        assert "tests" in data
        assert len(data["tests"]) > 0

    def test_load_list_alerts(self):
        data = fixture_loader.load("te_list_alerts")
        assert "alerts" in data

    def test_load_list_agents(self):
        data = fixture_loader.load("te_list_agents")
        assert "agents" in data
        assert len(data["agents"]) > 0

    def test_load_unknown_tool_returns_empty(self):
        data = fixture_loader.load("te_nonexistent_tool")
        assert data == {}

    def test_load_scenario_dns_failure(self):
        data = fixture_loader.load("te_get_test_results", scenario="dns_failure")
        assert data.get("_scenario") == "dns_failure"

    def test_load_scenario_saas_outage(self):
        data = fixture_loader.load("te_list_tests", scenario="saas_outage")
        assert data.get("_scenario") == "saas_outage"

    def test_fixture_mode_env(self, monkeypatch):
        monkeypatch.setenv("TE_USE_FIXTURES", "true")
        assert fixture_loader.fixture_mode_enabled() is True

    def test_fixture_mode_disabled(self, monkeypatch):
        monkeypatch.setenv("TE_USE_FIXTURES", "false")
        assert fixture_loader.fixture_mode_enabled() is False


# ---------------------------------------------------------------------------
# Normalizer — confidence scoring
# ---------------------------------------------------------------------------

class TestComputeNetworkConfidence:
    def test_full_outage_scores_high(self):
        ev = NetworkEvidence(availability=0.0, error_type="CONNECT")
        score = compute_network_confidence(ev)
        assert score >= 0.5

    def test_healthy_scores_low(self):
        ev = NetworkEvidence(availability=100.0, packet_loss=0.0)
        score = compute_network_confidence(ev)
        assert score == 0.0

    def test_packet_loss_adds_score(self):
        ev = NetworkEvidence(packet_loss=25.0)
        score = compute_network_confidence(ev)
        assert score >= 0.20

    def test_changed_hops_adds_score(self):
        ev = NetworkEvidence(changed_hops=2)
        score = compute_network_confidence(ev)
        assert score >= 0.10

    def test_global_scope_multiplier(self):
        ev_local = NetworkEvidence(availability=0.0, affected_scope="local")
        ev_global = NetworkEvidence(availability=0.0, affected_scope="global")
        assert compute_network_confidence(ev_global) > compute_network_confidence(ev_local)

    def test_score_capped_at_one(self):
        ev = NetworkEvidence(
            availability=0.0,
            packet_loss=50.0,
            error_type="CONNECT",
            connect_time_ms=1000.0,
            changed_hops=3,
            bgp_route_changed=True,
            dns_time_ms=600.0,
            affected_scope="global",
        )
        score = compute_network_confidence(ev)
        assert score <= 1.0

    def test_bgp_route_change_adds_score(self):
        ev = NetworkEvidence(bgp_route_changed=True)
        score = compute_network_confidence(ev)
        assert score >= 0.15


# ---------------------------------------------------------------------------
# Normalizer — owner inference
# ---------------------------------------------------------------------------

class TestInferOwner:
    def test_dns_failure_owner(self):
        ev = NetworkEvidence(
            test_type="dns-server",
            error_type="SERVER_ERROR",
        )
        assert infer_owner(ev) == "dns"

    def test_saas_outage_owner(self):
        ev = NetworkEvidence(
            response_code=503,
            affected_scope="global",
            agent_type="Cloud",
        )
        assert infer_owner(ev) == "saas"

    def test_enterprise_agent_degraded_owner(self):
        ev = NetworkEvidence(agent_type="Enterprise", availability=20.0)
        assert infer_owner(ev) == "endpoint"

    def test_changed_hops_owner(self):
        ev = NetworkEvidence(changed_hops=2)
        assert infer_owner(ev) == "isp"

    def test_packet_loss_owner(self):
        ev = NetworkEvidence(packet_loss=15.0)
        assert infer_owner(ev) == "isp"

    def test_unknown_owner(self):
        ev = NetworkEvidence()
        assert infer_owner(ev) == "unknown"


# ---------------------------------------------------------------------------
# Normalizer — normalize_alert + normalize_test_result
# ---------------------------------------------------------------------------

class TestNormalizeAlert:
    def test_normalize_alert_basic(self):
        raw = {
            "alertId": 987654,
            "testId": 123456,
            "testName": "API Gateway Health",
            "type": "http-server",
            "severity": "CRITICAL",
            "dateStart": "2026-06-10T10:03:00Z",
            "dateEnd": None,
            "agents": [{"agentId": 1, "availability": 0}, {"agentId": 2, "availability": 60}],
            "alertRule": {"alertRuleName": "Availability < 90%"},
        }
        ev = normalize_alert(raw)
        assert ev.test_id == "123456"
        assert ev.test_name == "API Gateway Health"
        assert ev.error_type == "TE_ALERT"
        assert ev.confidence > 0

    def test_normalize_alert_no_agents(self):
        raw = {"testId": 1, "testName": "test", "type": "http-server", "agents": []}
        ev = normalize_alert(raw)
        assert ev.availability is None


class TestNormalizeTestResult:
    def test_http_result_normalization(self):
        raw = {
            "agentId": 10001,
            "agentName": "New York, NY",
            "agentType": "Cloud",
            "availability": 0,
            "connectTime": 1200,
            "responseTime": 0,
            "responseCode": 0,
            "errorType": "CONNECT",
            "errorDetails": "Connection timed out",
        }
        ev = normalize_test_result(raw, "http-server")
        assert ev.availability == 0.0
        assert ev.connect_time_ms == 1200.0
        assert ev.error_type == "CONNECT"
        assert ev.agent_location == "New York, NY"

    def test_dns_result_normalization(self):
        raw = {
            "agentId": 10001,
            "agentName": "New York, NY",
            "availability": 0,
            "resolutionTime": 0,
            "errorType": "SERVER_ERROR",
            "errorDetails": "SERVFAIL",
        }
        ev = normalize_test_result(raw, "dns-server")
        assert ev.dns_time_ms == 0.0
        assert ev.error_type == "SERVER_ERROR"


class TestAggregateScope:
    def test_three_cloud_degraded_is_global(self):
        results = [
            NetworkEvidence(agent_type="Cloud", availability=0),
            NetworkEvidence(agent_type="Cloud", availability=0),
            NetworkEvidence(agent_type="Cloud", availability=0),
        ]
        assert aggregate_scope(results) == "global"

    def test_two_cloud_degraded_is_regional(self):
        results = [
            NetworkEvidence(agent_type="Cloud", availability=0),
            NetworkEvidence(agent_type="Cloud", availability=0),
        ]
        assert aggregate_scope(results) == "regional"

    def test_all_healthy_is_unknown(self):
        results = [NetworkEvidence(agent_type="Cloud", availability=100)]
        assert aggregate_scope(results) == "unknown"

    def test_empty_list(self):
        assert aggregate_scope([]) == "unknown"


# ---------------------------------------------------------------------------
# Correlation rules
# ---------------------------------------------------------------------------

class TestCorrelationRules:
    def _make_cloud_down(self, count=3) -> list[NetworkEvidence]:
        return [
            NetworkEvidence(agent_type="Cloud", availability=0.0, response_code=503)
            for _ in range(count)
        ]

    def test_rule_001_matches_packet_loss_high_connect(self):
        evidence = [NetworkEvidence(packet_loss=10.0, connect_time_ms=500.0)]
        result = rule_001_network_induced_latency(evidence, {})
        assert result.matched is True
        assert result.confidence_delta == pytest.approx(0.30)
        assert result.owner == "network"

    def test_rule_001_no_match_healthy(self):
        evidence = [NetworkEvidence(packet_loss=0.0, connect_time_ms=50.0)]
        result = rule_001_network_induced_latency(evidence, {})
        assert result.matched is False

    def test_rule_004_dns_failure_matches(self):
        evidence = [NetworkEvidence(test_type="dns-server", error_type="SERVER_ERROR")]
        result = rule_004_dns_root_cause(evidence, {})
        assert result.matched is True
        assert result.confidence_delta == pytest.approx(0.40)
        assert result.owner == "dns"

    def test_rule_004_no_match_http_error(self):
        evidence = [NetworkEvidence(test_type="http-server", error_type="HTTP_ERROR")]
        result = rule_004_dns_root_cause(evidence, {})
        assert result.matched is False

    def test_rule_006_saas_outage_matches(self):
        evidence = self._make_cloud_down(3)
        result = rule_006_saas_provider_outage(evidence, {})
        assert result.matched is True
        assert result.confidence_delta == pytest.approx(0.40)
        assert result.owner == "saas"

    def test_rule_006_no_match_single_agent(self):
        evidence = self._make_cloud_down(1)
        result = rule_006_saas_provider_outage(evidence, {})
        assert result.matched is False

    def test_run_all_rules_sorted_by_confidence(self):
        evidence = [
            NetworkEvidence(test_type="dns-server", error_type="SERVER_ERROR"),
            NetworkEvidence(agent_type="Cloud", availability=0.0, response_code=503),
            NetworkEvidence(agent_type="Cloud", availability=0.0, response_code=503),
            NetworkEvidence(agent_type="Cloud", availability=0.0, response_code=503),
        ]
        results = run_all_rules(evidence, {})
        assert len(results) >= 1
        # Results should be sorted descending by confidence_delta
        for i in range(len(results) - 1):
            assert results[i].confidence_delta >= results[i + 1].confidence_delta

    def test_run_all_rules_no_evidence(self):
        results = run_all_rules([], {})
        assert results == []


# ---------------------------------------------------------------------------
# Worker integration (fixture mode)
# ---------------------------------------------------------------------------

class TestThousandEyesWorkerFixtures:
    @pytest.fixture(autouse=True)
    def enable_with_fixtures(self, monkeypatch):
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "true")
        monkeypatch.setenv("TE_USE_FIXTURES", "true")

    def test_get_network_alerts_returns_structure(self):
        worker = ThousandEyesWorker()
        result = worker.execute("get_network_alerts", {})
        assert "network_evidence" in result
        assert "network_summary" in result
        assert isinstance(result["network_evidence"], list)

    def test_get_network_evidence_returns_structure(self):
        worker = ThousandEyesWorker()
        result = worker.execute("get_network_evidence", {})
        assert "network_evidence" in result
        assert "network_correlation" in result
        assert "network_summary" in result

    def test_network_summary_is_string(self):
        worker = ThousandEyesWorker()
        result = worker.execute("get_network_evidence", {})
        assert isinstance(result["network_summary"], str)

    def test_no_regression_flag_off(self, monkeypatch):
        """Turning the flag off mid-test should immediately gate all actions."""
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
        worker = ThousandEyesWorker()
        assert worker.execute("get_network_evidence", {}) == {}
        assert worker.execute("get_network_alerts", {}) == {}
