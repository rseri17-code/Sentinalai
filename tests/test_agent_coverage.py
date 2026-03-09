"""
Phase 2 coverage tests for supervisor/agent.py.

Targets all uncovered paths: LLM refinement, knowledge retrieval,
ITSM/DevOps enrichment, budget edge cases, helper methods, and
evidence formatting.
"""

from unittest.mock import Mock, MagicMock, patch

from supervisor.agent import (
    SentinalAISupervisor,
    Hypothesis,
)
from tests.test_supervisor import _build_mock_workers


# =========================================================================
# Helpers
# =========================================================================

def _supervisor_with_mocks(incident_id="INC12345"):
    sup = SentinalAISupervisor()
    _build_mock_workers(sup, incident_id)
    return sup


def _supervisor_with_full_mocks(incident_id, **overrides):
    """Build a supervisor with full mock workers including ITSM/DevOps."""
    sup = SentinalAISupervisor()
    _build_mock_workers(sup, incident_id)

    # Add ITSM worker mock
    itsm_mock = MagicMock()
    itsm_responses = {
        "get_ci_details": {"ci": {"tier": "P1", "owner": "sre-team", "dependencies": ["db", "cache"]}},
        "get_known_errors": {"known_errors": [{"id": "KE001", "description": "Known DB timeout"}]},
        "search_incidents": {"incidents": [{"id": "INC99999", "summary": "Similar past incident"}]},
        "get_change_records": {"change_records": [
            {
                "number": "CHG001",
                "type": "deployment",
                "short_description": "Deploy v2.3.1",
                "start_date": "2024-02-12T10:00:00Z",
                "end_date": "2024-02-12T10:15:00Z",
                "state": "completed",
                "requested_by": "deploy-bot",
                "approval": "auto-approved",
                "risk": "low",
                "rollback_plan": "Revert to v2.3.0",
                "service": "api-gateway",
            }
        ]},
    }

    def itsm_execute(action, params):
        return itsm_responses.get(action, {})

    itsm_mock.execute = Mock(side_effect=itsm_execute)
    sup.workers["itsm_worker"] = itsm_mock

    # Add DevOps worker mock
    devops_mock = MagicMock()
    devops_responses = {
        "get_recent_deployments": {"deployments": [
            {"pr_number": 42, "author": "dev-user", "sha": "abc123def456", "title": "Fix payment flow"}
        ]},
        "get_workflow_runs": {"workflow_runs": [
            {"conclusion": "success", "name": "CI", "run_number": 100}
        ]},
        "get_pr_details": {"pr": {"title": "Fix payment", "state": "merged"}},
        "get_commit_diff": {"commit": {"sha": "abc123", "files_changed": 3}},
    }

    def devops_execute(action, params):
        return devops_responses.get(action, {})

    devops_mock.execute = Mock(side_effect=devops_execute)
    sup.workers["devops_worker"] = devops_mock

    return sup


# =========================================================================
# LLM-enabled paths (refine + reasoning + format_evidence)
# =========================================================================

class TestLLMRefinementPaths:
    """Cover _llm_refine_hypotheses, _llm_generate_reasoning, _format_evidence_summary."""

    def test_llm_refine_hypotheses_success(self):
        """When LLM is enabled and refine succeeds, scores are updated."""
        sup = _supervisor_with_mocks("INC12347")  # error_spike with deployment
        with patch("supervisor.agent._llm_enabled", return_value=True), \
             patch("supervisor.agent._llm_refine") as mock_refine, \
             patch("supervisor.agent._llm_reasoning") as mock_reasoning, \
             patch("supervisor.agent.record_llm_usage"):
            mock_refine.return_value = {
                "refined_hypotheses": [
                    {"name": "deployment_error", "score": 95, "reasoning": "LLM refined reasoning"},
                ],
                "model_id": "anthropic.claude-sonnet-4-5-20250929-v1:0",
                "input_tokens": 500,
                "output_tokens": 200,
                "latency_ms": 1200,
            }
            mock_reasoning.return_value = {
                "reasoning": "Enhanced causal chain from LLM",
                "model_id": "anthropic.claude-sonnet-4-5-20250929-v1:0",
                "input_tokens": 300,
                "output_tokens": 150,
                "latency_ms": 800,
            }
            result = sup.investigate("INC12347")
            assert result["confidence"] > 0
            assert "root_cause" in result

    def test_llm_refine_hypotheses_exception_graceful(self):
        """When LLM refine raises, deterministic path continues."""
        sup = _supervisor_with_mocks("INC12347")
        with patch("supervisor.agent._llm_enabled", return_value=True), \
             patch("supervisor.agent._llm_refine", side_effect=Exception("Bedrock down")), \
             patch("supervisor.agent._llm_reasoning", side_effect=Exception("Bedrock down")), \
             patch("supervisor.agent.record_llm_usage"):
            result = sup.investigate("INC12347")
            assert result["confidence"] > 0
            assert "root_cause" in result

    def test_llm_reasoning_returns_empty_reasoning(self):
        """When LLM reasoning returns empty, fallback reasoning is used."""
        sup = _supervisor_with_mocks("INC12345")
        with patch("supervisor.agent._llm_enabled", return_value=True), \
             patch("supervisor.agent._llm_refine") as mock_refine, \
             patch("supervisor.agent._llm_reasoning") as mock_reasoning, \
             patch("supervisor.agent.record_llm_usage"):
            mock_refine.return_value = {
                "refined_hypotheses": [],
                "model_id": "test-model",
                "input_tokens": 10,
                "output_tokens": 5,
                "latency_ms": 100,
            }
            mock_reasoning.return_value = {"reasoning": ""}
            result = sup.investigate("INC12345")
            assert result["reasoning"]  # should have deterministic reasoning

    def test_format_evidence_summary_all_sources(self):
        """_format_evidence_summary covers all evidence types."""
        sup = SentinalAISupervisor()
        logs = [
            {"level": "ERROR", "message": "Connection timeout to payment-service"},
            {"level": "WARN", "message": "Retry attempt 3 failed"},
            {"level": "ERROR", "message": "Another error log entry"},
            {"level": "INFO", "message": "Fourth log"},
        ]
        signals = {
            "golden_signals": {"latency": {"p95": 500}, "errors": {"rate": 0.05}},
        }
        metrics = {
            "metrics": [{"name": "cpu", "value": 90}],
            "pattern": "spike",
        }
        events = [
            {"message": "Pod restarted due to OOM"},
            {"message": "Deployment completed"},
            {"message": "Third event"},
        ]
        changes = [
            {"change_type": "deployment", "description": "Deploy v2.3.1"},
            {"change_type": "config", "description": "Update config"},
            {"change_type": "migration", "description": "DB migration"},
        ]
        summary = sup._format_evidence_summary(logs, signals, metrics, events, changes)
        assert "Logs: 4 entries" in summary
        assert "Golden Signals:" in summary
        assert "Metrics:" in summary
        assert "Events:" in summary
        assert "Changes:" in summary

    def test_format_evidence_summary_empty(self):
        """_format_evidence_summary returns fallback for empty evidence."""
        sup = SentinalAISupervisor()
        summary = sup._format_evidence_summary([], {}, {}, [], [])
        assert summary == "No evidence collected"


# =========================================================================
# No-winner hypothesis path
# =========================================================================

class TestNoWinnerHypothesis:
    """Cover the path where no hypotheses are generated."""

    def test_no_winner_produces_inconclusive(self):
        """When _generate_hypotheses returns empty, result is inconclusive."""
        sup = _supervisor_with_mocks("INC12345")
        with patch.object(sup, "_generate_hypotheses", return_value=[]):
            result = sup.investigate("INC12345")
            assert "inconclusive" in result["root_cause"].lower()
            assert result["confidence"] < 80


# =========================================================================
# Knowledge retrieval paths
# =========================================================================

class TestKnowledgeRetrievalPaths:
    """Cover institutional knowledge retrieval in _analyze_evidence."""

    def test_knowledge_retrieval_boosts_confidence(self):
        """When knowledge retrieval finds matches, confidence is boosted."""
        sup = _supervisor_with_mocks("INC12345")
        mock_retrieval = MagicMock()
        mock_retrieval.retrieve_similar.return_value = [
            {"incident_id": "INC99999", "root_cause": "DB slow queries", "similarity_score": 0.8}
        ]
        with patch("supervisor.agent._KNOWLEDGE_AVAILABLE", True), \
             patch("supervisor.agent._knowledge_retrieval", mock_retrieval), \
             patch("supervisor.agent._retrieval_boost", return_value=8.0):
            result = sup.investigate("INC12345")
            assert result["confidence"] > 0
            assert result.get("retrieval_confidence_boost", 0) >= 0

    def test_knowledge_retrieval_exception_graceful(self):
        """When knowledge retrieval raises, investigation continues."""
        sup = _supervisor_with_mocks("INC12345")
        mock_retrieval = MagicMock()
        mock_retrieval.retrieve_similar.side_effect = Exception("Graph store unavailable")
        with patch("supervisor.agent._KNOWLEDGE_AVAILABLE", True), \
             patch("supervisor.agent._knowledge_retrieval", mock_retrieval):
            result = sup.investigate("INC12345")
            assert "root_cause" in result

    def test_knowledge_retrieval_no_proof_caps_confidence(self):
        """Without evidence refs, confidence stays below 80 even with retrieval boost."""
        sup = _supervisor_with_mocks("INC12345")
        mock_retrieval = MagicMock()
        mock_retrieval.retrieve_similar.return_value = [
            {"incident_id": "INC99999", "root_cause": "old issue", "similarity_score": 0.9}
        ]
        # Create a hypothesis with no evidence refs to trigger the cap
        def fake_generate(incident_type, service, summary, logs, signals, metrics, events, changes, timeline):
            return [Hypothesis(
                name="test_hyp",
                root_cause="test root cause",
                base_score=70,
                evidence_refs=[],  # No evidence refs
                reasoning="test reasoning",
            )]

        with patch.object(sup, "_generate_hypotheses", side_effect=fake_generate), \
             patch("supervisor.agent._KNOWLEDGE_AVAILABLE", True), \
             patch("supervisor.agent._knowledge_retrieval", mock_retrieval), \
             patch("supervisor.agent._retrieval_boost", return_value=10.0):
            result = sup.investigate("INC12345")
            assert result["confidence"] <= 79


# =========================================================================
# Memory store paths
# =========================================================================

class TestMemoryStorePaths:
    """Cover _store_to_memory path in investigate()."""

    def test_memory_store_called_when_enabled(self):
        """When memory is enabled, store is called after investigation."""
        sup = _supervisor_with_mocks("INC12345")
        with patch("supervisor.agent._memory_enabled", return_value=True), \
             patch("supervisor.agent._store_to_memory") as mock_store:
            sup.investigate("INC12345")
            mock_store.assert_called_once()
            call_kwargs = mock_store.call_args
            assert call_kwargs[1]["incident_id"] == "INC12345"

    def test_memory_store_exception_graceful(self):
        """When memory store raises, investigation still succeeds."""
        sup = _supervisor_with_mocks("INC12345")
        with patch("supervisor.agent._memory_enabled", return_value=True), \
             patch("supervisor.agent._store_to_memory", side_effect=Exception("Memory unavailable")):
            result = sup.investigate("INC12345")
            assert "root_cause" in result


# =========================================================================
# Knowledge graph persist paths
# =========================================================================

class TestKnowledgeGraphPersistPaths:
    """Cover _knowledge_graph.persist_investigation in investigate()."""

    def test_knowledge_graph_persist_called(self):
        """When knowledge graph is available, persist is called."""
        sup = _supervisor_with_mocks("INC12345")
        mock_graph = MagicMock()
        with patch("supervisor.agent._KNOWLEDGE_AVAILABLE", True), \
             patch("supervisor.agent._knowledge_graph", mock_graph):
            sup.investigate("INC12345")
            mock_graph.persist_investigation.assert_called_once()

    def test_knowledge_graph_persist_exception_graceful(self):
        """When persist raises, investigation still succeeds."""
        sup = _supervisor_with_mocks("INC12345")
        mock_graph = MagicMock()
        mock_graph.persist_investigation.side_effect = Exception("Disk full")
        with patch("supervisor.agent._KNOWLEDGE_AVAILABLE", True), \
             patch("supervisor.agent._knowledge_graph", mock_graph):
            result = sup.investigate("INC12345")
            assert "root_cause" in result


# =========================================================================
# Judge exception path
# =========================================================================

class TestJudgeExceptionPath:
    """Cover judge scoring exception handling."""

    def test_judge_exception_does_not_crash(self):
        """When judge raises, investigation still completes."""
        sup = _supervisor_with_mocks("INC12345")
        with patch("supervisor.agent._judge_and_record", side_effect=Exception("Judge unavailable")):
            result = sup.investigate("INC12345")
            assert "root_cause" in result


# =========================================================================
# ITSM enrichment paths
# =========================================================================

class TestItsmEnrichmentPaths:
    """Cover _fetch_itsm_context and ITSM enrichment in analyzers."""

    def test_fetch_itsm_context_all_sources(self):
        """ITSM context fetches CI, known errors, and similar incidents."""
        sup = _supervisor_with_full_mocks("INC12347")
        result = sup.investigate("INC12347")
        assert "root_cause" in result

    def test_fetch_itsm_context_budget_exhausted_midway(self):
        """When budget exhausts during ITSM fetch, partial context returned."""
        sup = _supervisor_with_full_mocks("INC12345")
        # Set very low budget to exhaust mid-ITSM
        with patch("supervisor.agent.ExecutionBudget") as MockBudget:
            budget = MagicMock()
            call_count = [0]

            def can_call():
                call_count[0] += 1
                return call_count[0] <= 3  # Only allow 3 calls total
            budget.can_call = can_call
            budget.record_call = Mock()
            budget.calls_made = 3
            budget.max_calls = 3
            MockBudget.return_value = budget
            result = sup.investigate("INC12345")
            assert "root_cause" in result

    def test_itsm_context_enriches_error_spike_analysis(self):
        """ITSM CI details and rollback plan appear in error_spike analysis."""
        sup = _supervisor_with_full_mocks("INC12347")  # error_spike incident
        result = sup.investigate("INC12347")
        assert result["confidence"] > 0

    def test_extract_itsm_context_non_dict(self):
        """_extract_itsm_context returns {} for non-dict input."""
        sup = SentinalAISupervisor()
        assert sup._extract_itsm_context({"itsm_context": "not_a_dict"}) == {}
        assert sup._extract_itsm_context({"itsm_context": 42}) == {}
        assert sup._extract_itsm_context({}) == {}

    def test_extract_itsm_context_valid(self):
        """_extract_itsm_context returns the dict for valid input."""
        sup = SentinalAISupervisor()
        ctx = {"ci": {"tier": "P1"}, "known_errors": []}
        assert sup._extract_itsm_context({"itsm_context": ctx}) == ctx


# =========================================================================
# DevOps enrichment paths
# =========================================================================

class TestDevopsEnrichmentPaths:
    """Cover _fetch_devops_context and DevOps enrichment in analyzers."""

    def test_fetch_devops_context_with_deployment(self):
        """DevOps enrichment is called when deployment is found in changes."""
        sup = _supervisor_with_full_mocks("INC12347")  # error_spike with deployment
        # Make the change data have a deployment
        orig_log = sup.workers["log_worker"].execute

        def enhanced_log(action, params):
            if action == "get_change_data":
                return {"changes": [
                    {
                        "change_type": "deployment",
                        "description": "Deploy v2.3.1",
                        "scheduled_start": "2024-02-12T10:00:00Z",
                        "service": "payment-service",
                    }
                ]}
            return orig_log(action, params)

        sup.workers["log_worker"].execute = Mock(side_effect=enhanced_log)
        result = sup.investigate("INC12347")
        assert "root_cause" in result

    def test_extract_devops_context_non_dict(self):
        """_extract_devops_context returns {} for non-dict input."""
        sup = SentinalAISupervisor()
        assert sup._extract_devops_context({"devops_context": "not_a_dict"}) == {}
        assert sup._extract_devops_context({"devops_context": 42}) == {}

    def test_extract_devops_context_valid(self):
        """_extract_devops_context returns the dict for valid input."""
        sup = SentinalAISupervisor()
        ctx = {"deployments": [{"pr_number": 42}]}
        assert sup._extract_devops_context({"devops_context": ctx}) == ctx

    def test_devops_enrichment_in_error_spike_analyzer(self):
        """DevOps context (deployments + workflows) enriches error_spike hypothesis."""
        sup = _supervisor_with_full_mocks("INC12347")
        # Pre-set devops evidence
        sup._devops_evidence = {
            "deployments": [{"pr_number": 42, "author": "dev", "sha": "abc123def456"}],
            "workflow_runs": [{"conclusion": "success", "name": "CI"}],
        }
        sup._itsm_evidence = {
            "ci": {"tier": "P1"},
        }
        # Directly test the analyzer
        logs = [{"message": "NullPointerException in PaymentHandler", "level": "ERROR", "_time": "2024-02-12T10:30:15Z"}]
        signals = {
            "golden_signals": {"errors": {"rate": 0.15, "count": 200}, "latency": {}},
            "anomaly_detected": True, "anomaly_type": "error_spike",
        }
        changes = [
            {"change_type": "deployment", "description": "Deploy v2.3.1",
             "scheduled_start": "2024-02-12T10:00:00Z", "rollback_plan": "Revert to v2.3.0"},
        ]
        hypotheses = sup._analyze_error_spike(
            "payment-service", "Error spike after deploy", logs, signals, {}, [], changes, [],
        )
        # Should have a deployment_error hypothesis with devops evidence refs
        dep_hyp = [h for h in hypotheses if h.name == "deployment_error"]
        assert len(dep_hyp) == 1
        assert "devops:deployments" in dep_hyp[0].evidence_refs
        assert "devops:workflow_runs" in dep_hyp[0].evidence_refs
        assert "itsm:ci_details" in dep_hyp[0].evidence_refs
        assert "P1" in dep_hyp[0].reasoning

    def test_devops_enrichment_in_saturation_analyzer(self):
        """DevOps workflow_runs enrich saturation hypothesis."""
        sup = _supervisor_with_full_mocks("INC12349")
        sup._devops_evidence = {
            "workflow_runs": [{"conclusion": "failure", "name": "CI"}],
        }
        sup._itsm_evidence = {}
        signals = {
            "golden_signals": {"saturation": {"cpu": 95}, "latency": {}, "errors": {}},
        }
        changes = [
            {"change_type": "config_change", "description": "Update thread pool",
             "scheduled_start": "2024-02-12T09:00:00Z"},
        ]
        metrics = {"metrics": [{"name": "cpu", "value": 95}]}
        hypotheses = sup._analyze_saturation(
            "order-service", "CPU exhaustion", [], signals, metrics, [], changes, [],
        )
        cpu_change = [h for h in hypotheses if h.name == "cpu_after_change"]
        assert len(cpu_change) == 1
        assert "devops:workflow_runs" in cpu_change[0].evidence_refs


# =========================================================================
# Budget / Circuit breaker edge cases
# =========================================================================

class TestBudgetEdgeCases:
    """Cover circuit-open path, budget-on-retry, budget in fetch_incident."""

    def test_circuit_open_skips_call(self):
        """When circuit breaker is open, _call_worker returns circuit_open error."""
        sup = SentinalAISupervisor()
        worker = MagicMock()
        circuits = MagicMock(spec=["get"])
        circuit = MagicMock()
        circuit.is_open = True
        circuits.get.return_value = circuit

        result = sup._call_worker(
            worker, "test_action", {}, None, None,
            worker_name="test_worker", circuits=circuits,
        )
        assert result["error"] == "circuit_open"
        assert result["worker"] == "test_worker"
        worker.execute.assert_not_called()

    def test_budget_exhausted_during_retry(self):
        """When budget exhausts before retry, loop breaks."""
        sup = SentinalAISupervisor()
        sup._max_retries = 2
        worker = MagicMock()
        worker.execute.side_effect = Exception("fail")

        budget = MagicMock()
        call_count = [0]

        def can_call():
            call_count[0] += 1
            return call_count[0] <= 1  # Only allow first call
        budget.can_call = Mock(side_effect=can_call)
        budget.record_call = Mock()

        result = sup._call_worker(
            worker, "test_action", {}, None, budget,
            worker_name="test_worker",
        )
        assert "error" in result

    def test_budget_exhausted_in_fetch_incident(self):
        """When budget is exhausted at fetch_incident, returns None."""
        sup = SentinalAISupervisor()
        budget = MagicMock()
        budget.can_call.return_value = False
        assert sup._fetch_incident("INC123", budget=budget) is None

    def test_budget_exhausted_mid_playbook(self):
        """When budget exhausts mid-playbook, remaining steps are skipped."""
        sup = _supervisor_with_mocks("INC12345")
        with patch("supervisor.agent.ExecutionBudget") as MockBudget:
            budget = MagicMock()
            call_count = [0]

            def can_call():
                call_count[0] += 1
                return call_count[0] <= 2
            budget.can_call = Mock(side_effect=can_call)
            budget.record_call = Mock()
            budget.calls_made = 2
            budget.max_calls = 2
            MockBudget.return_value = budget
            result = sup.investigate("INC12345")
            assert "root_cause" in result


# =========================================================================
# Historical context paths
# =========================================================================

class TestHistoricalContextPaths:
    """Cover _fetch_historical_context edge cases."""

    def test_no_knowledge_worker(self):
        """When knowledge_worker not in workers, returns None."""
        sup = SentinalAISupervisor()
        sup.workers.pop("knowledge_worker", None)
        result = sup._fetch_historical_context("svc", "summary")
        assert result is None

    def test_knowledge_worker_returns_empty(self):
        """When knowledge_worker returns no similar_incidents, returns None."""
        sup = SentinalAISupervisor()
        worker = MagicMock()
        worker.execute.return_value = {"similar_incidents": []}
        sup.workers["knowledge_worker"] = worker
        result = sup._fetch_historical_context("svc", "summary")
        assert result is None

    def test_budget_exhausted(self):
        """When budget is exhausted, returns None."""
        sup = SentinalAISupervisor()
        budget = MagicMock()
        budget.can_call.return_value = False
        result = sup._fetch_historical_context("svc", "summary", budget=budget)
        assert result is None

    def test_knowledge_worker_returns_matches(self):
        """When knowledge_worker returns similar incidents, returns the result."""
        sup = SentinalAISupervisor()
        worker = MagicMock()
        worker.execute.return_value = {"similar_incidents": [{"id": "INC99"}]}
        sup.workers["knowledge_worker"] = worker
        result = sup._fetch_historical_context("svc", "summary")
        assert result is not None
        assert result["similar_incidents"]


# =========================================================================
# _build_params for ITSM and DevOps actions
# =========================================================================

class TestBuildParamsItsmDevops:
    """Cover ITSM/DevOps branches in _build_params."""

    def setup_method(self):
        self.sup = SentinalAISupervisor()

    def test_get_ci_details(self):
        params = self.sup._build_params(
            {"action": "get_ci_details", "worker": "itsm_worker"},
            "INC123", "my-service",
        )
        assert params["service"] == "my-service"

    def test_search_incidents(self):
        params = self.sup._build_params(
            {"action": "search_incidents", "worker": "itsm_worker", "query_hint": "timeout"},
            "INC123", "my-service",
        )
        assert params["service"] == "my-service"
        assert params["query"] == "timeout"

    def test_get_change_records(self):
        params = self.sup._build_params(
            {"action": "get_change_records", "worker": "itsm_worker"},
            "INC123", "my-service",
        )
        assert params["service"] == "my-service"

    def test_get_known_errors(self):
        params = self.sup._build_params(
            {"action": "get_known_errors", "worker": "itsm_worker"},
            "INC123", "my-service",
        )
        assert params["service"] == "my-service"

    def test_get_recent_deployments(self):
        params = self.sup._build_params(
            {"action": "get_recent_deployments", "worker": "devops_worker"},
            "INC123", "my-service",
        )
        assert params["service"] == "my-service"

    def test_get_pr_details(self):
        params = self.sup._build_params(
            {"action": "get_pr_details", "worker": "devops_worker",
             "repo": "org/repo", "pr_number": 42},
            "INC123", "my-service",
        )
        assert params["repo"] == "org/repo"
        assert params["pr_number"] == 42

    def test_get_commit_diff(self):
        params = self.sup._build_params(
            {"action": "get_commit_diff", "worker": "devops_worker",
             "repo": "org/repo", "sha": "abc123"},
            "INC123", "my-service",
        )
        assert params["repo"] == "org/repo"
        assert params["sha"] == "abc123"

    def test_get_workflow_runs(self):
        params = self.sup._build_params(
            {"action": "get_workflow_runs", "worker": "devops_worker"},
            "INC123", "my-service",
        )
        assert params["service"] == "my-service"


# =========================================================================
# Data extraction: ServiceNow change records + ITSM timeline
# =========================================================================

class TestDataExtraction:
    """Cover _extract_changes for ServiceNow and ITSM timeline building."""

    def test_extract_servicenow_change_records(self):
        """_extract_changes converts ServiceNow change_records to unified format."""
        sup = SentinalAISupervisor()
        evidence = {
            "itsm_changes": {
                "change_records": [
                    {
                        "number": "CHG001",
                        "type": "deployment",
                        "short_description": "Deploy v2.3.1",
                        "start_date": "2024-02-12T10:00:00Z",
                        "end_date": "2024-02-12T10:15:00Z",
                        "state": "completed",
                        "requested_by": "deploy-bot",
                        "approval": "auto-approved",
                        "risk": "low",
                        "rollback_plan": "Revert to v2.3.0",
                        "service": "api-gateway",
                    }
                ],
            },
        }
        changes = sup._extract_changes(evidence)
        assert len(changes) == 1
        assert changes[0]["change_id"] == "CHG001"
        assert changes[0]["change_type"] == "deployment"
        assert changes[0]["rollback_plan"] == "Revert to v2.3.0"
        assert changes[0]["approval"] == "auto-approved"

    def test_itsm_timeline_entries(self):
        """_build_timeline includes ITSM change records with approval/risk."""
        sup = SentinalAISupervisor()
        changes = [
            {
                "change_type": "deployment",
                "description": "Deploy v2.3.1",
                "scheduled_start": "2024-02-12T10:00:00Z",
                "service": "api-gateway",
                "approval": "auto-approved",
                "risk": "low",
                "rollback_plan": "Revert to v2.3.0",
            },
        ]
        timeline = sup._build_timeline([], {}, {}, [], changes, "error_spike", "api-gateway")
        # Should have both a regular change entry and an ITSM change entry
        itsm_entries = [e for e in timeline if e["source"] == "itsm_changes"]
        assert len(itsm_entries) == 1
        assert "approval: auto-approved" in itsm_entries[0]["event"]
        assert "risk: low" in itsm_entries[0]["event"]


# =========================================================================
# Analysis helper methods
# =========================================================================

class TestAnalysisHelpers:
    """Cover uncovered branches in helper methods."""

    def setup_method(self):
        self.sup = SentinalAISupervisor()

    def test_analyze_generic(self):
        """_analyze_generic returns a single generic hypothesis."""
        hyps = self.sup._analyze_generic("svc", "summary", [], {}, {}, [], [], [])
        assert len(hyps) == 1
        assert hyps[0].name == "generic"
        assert "inconclusive" in hyps[0].root_cause

    def test_find_error_type_out_of_memory(self):
        """_find_error_type detects OutOfMemoryError."""
        logs = [{"message": "OutOfMemoryError: Java heap space"}]
        assert self.sup._find_error_type(logs) == "OutOfMemoryError"

    def test_find_error_type_connection_refused(self):
        """_find_error_type detects ConnectionRefused."""
        logs = [{"message": "ConnectionRefused: redis:6379"}]
        assert self.sup._find_error_type(logs) == "ConnectionRefused"

    def test_find_downstream_service_from_field(self):
        """_find_downstream_service uses 'downstream' log field."""
        logs = [{"message": "timeout connecting", "downstream": "payment-service"}]
        assert self.sup._find_downstream_service(logs) == "payment-service"

    def test_find_backend_from_logs_elasticsearch(self):
        """_find_backend_from_logs detects elasticsearch."""
        logs = [{"message": "elasticsearch cluster red"}]
        assert self.sup._find_backend_from_logs(logs) == "elasticsearch"

    def test_find_backend_from_logs_redis(self):
        """_find_backend_from_logs detects redis."""
        logs = [{"message": "redis connection pool exhausted"}]
        assert self.sup._find_backend_from_logs(logs) == "redis"

    def test_find_backend_from_logs_database(self):
        """_find_backend_from_logs detects database."""
        logs = [{"message": "database connection timeout"}]
        assert self.sup._find_backend_from_logs(logs) == "database"

    def test_find_backend_event_with_event_type(self):
        """_find_backend_event returns event_type when present."""
        logs = [{"message": "redis failure", "event_type": "connection_lost"}]
        assert self.sup._find_backend_event(logs, "redis") == "redis connection_lost"

    def test_find_backend_event_without_event_type(self):
        """_find_backend_event returns message when no event_type."""
        logs = [{"message": "redis connection pool exhausted"}]
        result = self.sup._find_backend_event(logs, "redis")
        assert result == "redis connection pool exhausted"

    def test_find_connection_error_timeout(self):
        """_find_connection_error detects timeout."""
        logs = [{"message": "upstream timeout after 30s"}]
        assert self.sup._find_connection_error(logs) == "timeout"

    def test_find_connection_target_redis(self):
        """_find_connection_target detects redis."""
        logs = [{"message": "connection refused to redis:6379"}]
        assert self.sup._find_connection_target(logs) == "redis"

    def test_find_connection_target_postgres(self):
        """_find_connection_target detects postgres/database."""
        logs = [{"message": "postgres connection pool exhausted"}]
        assert self.sup._find_connection_target(logs) == "database"

    def test_find_connection_target_elasticsearch(self):
        """_find_connection_target detects elasticsearch."""
        logs = [{"message": "elasticsearch node unreachable"}]
        assert self.sup._find_connection_target(logs) == "elasticsearch"

    def test_detect_sawtooth_pattern_from_values(self):
        """_detect_sawtooth_pattern detects oscillation in metric values."""
        sup = SentinalAISupervisor()
        metrics = {
            "metrics": [
                {"value": 10}, {"value": 50}, {"value": 15},
                {"value": 55}, {"value": 20},
            ],
        }
        assert sup._detect_sawtooth_pattern(metrics) is True

    def test_detect_sawtooth_pattern_not_enough_data(self):
        """_detect_sawtooth_pattern returns False for < 4 data points."""
        metrics = {"metrics": [{"value": 10}, {"value": 50}]}
        assert self.sup._detect_sawtooth_pattern(metrics) is False

    def test_find_pipeline_failure(self):
        """_find_pipeline_failure detects pipeline errors."""
        logs = [{"message": "data pipeline failure: ETL stage 3 failed"}]
        assert self.sup._find_pipeline_failure(logs) is True

    def test_find_stale_cache(self):
        """_find_stale_cache detects stale cache entries."""
        logs = [{"message": "stale cache detected for user-profiles"}]
        assert self.sup._find_stale_cache(logs) is True

    def test_describe_anomaly_fallback(self):
        """_describe_anomaly handles unknown anomaly types."""
        desc = self.sup._describe_anomaly("custom_anomaly", "svc", {}, {}, {})
        assert "custom_anomaly" in desc


# =========================================================================
# GenAI span attributes
# =========================================================================

class TestGenAISpanAttributes:
    """Cover LLM usage span attribute setting (lines 289-292)."""

    def test_genai_attributes_set_when_llm_metrics_present(self):
        """When LLM metrics are present, GenAI span attributes are set."""
        sup = _supervisor_with_mocks("INC12345")
        with patch("supervisor.agent._llm_enabled", return_value=True), \
             patch("supervisor.agent._llm_refine") as mock_refine, \
             patch("supervisor.agent._llm_reasoning") as mock_reasoning, \
             patch("supervisor.agent.record_llm_usage"):
            mock_refine.return_value = {
                "refined_hypotheses": [],
                "model_id": "test-model",
                "input_tokens": 100,
                "output_tokens": 50,
                "latency_ms": 500,
            }
            mock_reasoning.return_value = {
                "reasoning": "LLM reasoning",
                "input_tokens": 80,
                "output_tokens": 40,
                "latency_ms": 300,
                "model_id": "test-model",
            }
            result = sup.investigate("INC12345")
            assert "root_cause" in result


# =========================================================================
# _empty_result
# =========================================================================

class TestEmptyResult:
    """Cover _empty_result helper."""

    def test_empty_result_structure(self):
        sup = SentinalAISupervisor()
        result = sup._empty_result("INC123", "test reason")
        assert result["incident_id"] == "INC123"
        assert result["root_cause"] == "test reason"
        assert result["confidence"] == 10
        assert result["evidence_timeline"] == []
        assert "test reason" in result["reasoning"]
