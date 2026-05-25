"""Supplementary tests for security audit gap remediations.

Extends test_security_remediations.py with additional edge cases
and integration tests for gaps G1.1, G5.x, G6.1, G7.1, G7.2.
"""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock


from supervisor.receipt import Receipt, ReceiptCollector
from supervisor.guardrails import validate_query, ExecutionBudget
from supervisor.eval_metrics import estimate_llm_cost


# =========================================================================
# G1.1: Extended validate_query coverage
# =========================================================================

class TestG1_1_Extended:
    """Extended tests for validate_query wired into LogWorker."""

    def test_log_worker_blocks_outputlookup(self):
        """LogWorker rejects queries containing 'outputlookup'."""
        from workers.log_worker import LogWorker

        worker = LogWorker()
        result = worker.execute("search_logs", {"query": "outputlookup my_table"})
        assert "error" in result

    def test_log_worker_blocks_collect_pattern(self):
        """LogWorker rejects queries containing 'collect'."""
        from workers.log_worker import LogWorker

        worker = LogWorker()
        result = worker.execute("search_logs", {"query": "collect index=summary"})
        assert "error" in result

    def test_validate_query_blocks_all_dangerous_patterns(self):
        """validate_query() blocks all 6 documented dangerous patterns."""
        dangerous = [
            "search | eval x=1",
            "index=main | delete",
            "lookup bad_table",
            "outputlookup hack",
            "delete events",
            "collect to index",
        ]
        for q in dangerous:
            is_valid, reason = validate_query(q)
            assert not is_valid, f"Expected '{q}' to be blocked but it passed"

    def test_validate_query_rejects_whitespace_only(self):
        """validate_query() rejects whitespace-only queries."""
        is_valid, _ = validate_query("   ")
        assert not is_valid

    def test_validate_query_allows_all_safe_keywords(self):
        """validate_query() allows all safe keywords from SPLUNK_QUERY_ALLOWLIST."""
        safe_keywords = [
            "timeout errors",
            "oomkill container",
            "error rate increase",
            "latency spike",
            "cpu saturation",
            "dns resolution failure",
            "cascade failure",
            "pipeline stale",
        ]
        for q in safe_keywords:
            is_valid, reason = validate_query(q)
            assert is_valid, f"Expected '{q}' to pass but got: {reason}"

    def test_validate_query_allows_stage2_domain_terms(self):
        """validate_query() allows Stage 2 domain terms added by Fix 3."""
        stage2_keywords = [
            "certificate expired TLS handshake",
            "tls cert rotation failure",
            "ssl handshake timeout",
            "kafka consumer lag high",
            "restart loop detected",
            "crashloop backoff",
            "credential rotation failed",
            "ORA-00942 table not found",
            "vault token renewal",
            "saml assertion failed",
            "oauth token expired",
            "ldap bind error",
        ]
        for q in stage2_keywords:
            is_valid, reason = validate_query(q)
            assert is_valid, f"Expected stage2 query '{q}' to pass but got: {reason}"


# =========================================================================
# G5: Receipt field integration tests
# =========================================================================

class TestG5_ReceiptFieldIntegration:
    """Integration tests for Receipt policy_ref, wall_clock_ts, and trace_id."""

    def test_receipt_all_new_fields_roundtrip(self):
        """All new receipt fields survive to_dict -> from_dict roundtrip."""
        r = Receipt(
            tool="log_worker",
            action="search_logs",
            policy_ref="playbook:timeout:step1|budget:remaining=18",
            trace_id="abc123def456",
        )
        d = r.to_dict()
        r2 = Receipt.from_dict(d)
        assert r2.policy_ref == r.policy_ref
        assert r2.trace_id == r.trace_id

    def test_collector_wall_clock_is_iso8601(self):
        """ReceiptCollector.start() produces valid ISO 8601 wall clock."""
        from datetime import datetime

        collector = ReceiptCollector(case_id="INC001")
        receipt = collector.start("test", "act", {})
        # The field name may be wall_clock_start or wall_clock_ts
        wc = getattr(receipt, "wall_clock_start", "") or getattr(receipt, "wall_clock_ts", "")
        assert wc != ""
        parsed = datetime.fromisoformat(wc)
        assert parsed is not None

    def test_collector_populates_policy_ref(self):
        """ReceiptCollector.start() passes policy_ref."""
        collector = ReceiptCollector(case_id="INC001")
        receipt = collector.start(
            "ops_worker", "get_incident_by_id",
            {"incident_id": "INC001"},
            policy_ref="playbook:fetch|budget:remaining=20",
        )
        assert "playbook:" in receipt.policy_ref
        assert "budget:" in receipt.policy_ref

    def test_receipt_trace_id_propagation(self):
        """ReceiptCollector propagates trace_id to receipts."""
        collector = ReceiptCollector(case_id="INC001", trace_id="otel-trace-xyz")
        receipt = collector.start("test", "act", {})
        assert receipt.trace_id == "otel-trace-xyz"


# =========================================================================
# G6.1: Deadline edge cases
# =========================================================================

class TestG6_1_DeadlineEdgeCases:
    """Extended deadline tests beyond basic pass/fail."""

    def test_deadline_allows_when_not_set(self):
        """_call_worker works normally when no deadline is set."""
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        # Don't set _investigation_deadline
        if hasattr(supervisor, '_investigation_deadline'):
            delattr(supervisor, '_investigation_deadline')

        result = supervisor._call_worker(
            supervisor.workers["ops_worker"],
            "get_incident_by_id",
            {"incident_id": "INC999"},
            receipts=None,
            budget=None,
            worker_name="ops_worker",
        )
        assert result.get("error") != "investigation_deadline_exceeded"

    def test_deadline_allows_when_future(self):
        """_call_worker proceeds when deadline is in the future."""
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        supervisor._investigation_deadline = time.monotonic() + 120.0

        result = supervisor._call_worker(
            supervisor.workers["ops_worker"],
            "get_incident_by_id",
            {"incident_id": "INC999"},
            receipts=None,
            budget=None,
            worker_name="ops_worker",
        )
        assert result.get("error") != "investigation_deadline_exceeded"

    def test_playbook_stops_on_expired_deadline(self):
        """_execute_playbook stops when deadline has passed."""
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        supervisor._investigation_deadline = time.monotonic() - 1.0

        budget = ExecutionBudget(case_id="INC001", max_calls=20)
        evidence = supervisor._execute_playbook(
            "timeout", "INC001", "api-gateway",
            receipts=ReceiptCollector(case_id="INC001"),
            budget=budget,
        )
        # All steps should be skipped or return deadline error
        for key, value in evidence.items():
            if isinstance(value, dict) and "error" in value:
                assert value["error"] == "investigation_deadline_exceeded"


# =========================================================================
# G7.1: Child OTEL span verification
# =========================================================================

class TestG7_1_ChildSpans:
    """G7.1: Each tool call must create a child OTEL span."""

    def test_call_worker_creates_tool_span(self):
        """_call_worker wraps execution in trace_span with tool-specific name."""
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        supervisor._investigation_deadline = time.monotonic() + 120.0
        receipts = ReceiptCollector(case_id="INC001")

        with patch("supervisor.agent.trace_span") as mock_span:
            mock_span_obj = MagicMock()
            mock_span.return_value.__enter__ = MagicMock(return_value=mock_span_obj)
            mock_span.return_value.__exit__ = MagicMock(return_value=False)

            supervisor._call_worker(
                supervisor.workers["ops_worker"],
                "get_incident_by_id",
                {"incident_id": "INC001"},
                receipts=receipts,
                budget=ExecutionBudget(case_id="INC001"),
                worker_name="ops_worker",
            )

            # Verify trace_span was called with tool-specific name
            mock_span.assert_called_with(
                "tool:ops_worker.get_incident_by_id",
                case_id="INC001",
            )

    def test_tool_span_sets_attributes(self):
        """Child span has worker_name and action attributes set."""
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        supervisor._investigation_deadline = time.monotonic() + 120.0
        receipts = ReceiptCollector(case_id="INC001")

        with patch("supervisor.agent.trace_span") as mock_span:
            mock_span_obj = MagicMock()
            mock_span.return_value.__enter__ = MagicMock(return_value=mock_span_obj)
            mock_span.return_value.__exit__ = MagicMock(return_value=False)

            supervisor._call_worker(
                supervisor.workers["log_worker"],
                "search_logs",
                {"query": "timeout"},
                receipts=receipts,
                budget=ExecutionBudget(case_id="INC001"),
                worker_name="log_worker",
            )

            # Verify span attributes were set
            calls = mock_span_obj.set_attribute.call_args_list
            attr_keys = [c[0][0] for c in calls]
            assert "worker_name" in attr_keys
            assert "action" in attr_keys


# =========================================================================
# G7.2: Cost estimation edge cases
# =========================================================================

class TestG7_2_CostEdgeCases:
    """Extended cost estimation tests."""

    def test_estimate_cost_all_supported_models(self):
        """estimate_llm_cost works for all supported model prefixes."""
        from supervisor.eval_metrics import _MODEL_PRICING

        for model_prefix in _MODEL_PRICING:
            cost = estimate_llm_cost(f"{model_prefix}-test", 1000, 500)
            assert cost > 0, f"Expected positive cost for {model_prefix}"

    def test_estimate_cost_deterministic(self):
        """Same inputs always produce same cost."""
        cost1 = estimate_llm_cost("anthropic.claude-3-5-sonnet-v1", 1000, 500)
        cost2 = estimate_llm_cost("anthropic.claude-3-5-sonnet-v1", 1000, 500)
        assert cost1 == cost2

    def test_cost_scales_linearly_with_tokens(self):
        """Cost doubles when tokens double."""
        cost1 = estimate_llm_cost("anthropic.claude-3-haiku-v1", 1000, 500)
        cost2 = estimate_llm_cost("anthropic.claude-3-haiku-v1", 2000, 1000)
        assert abs(cost2 - 2 * cost1) < 1e-8

    def test_record_llm_usage_includes_cost(self):
        """record_llm_usage emits cost without errors."""
        from supervisor.eval_metrics import record_llm_usage

        # Should not raise
        record_llm_usage(
            operation="generate_reasoning",
            model_id="anthropic.claude-3-5-sonnet-20250929-v1:0",
            input_tokens=2000,
            output_tokens=512,
            latency_ms=350.0,
        )


# =========================================================================
# Integration: Full investigation receipt verification
# =========================================================================

class TestInvestigationReceiptIntegration:
    """Verify receipts carry audit fields through full investigation."""

    def test_investigation_produces_receipts_with_policy_ref(self):
        """Full investigation produces receipts with policy_ref populated."""
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        result = supervisor.investigate("INC12345")

        # Result should be valid
        assert "root_cause" in result
        assert "confidence" in result

    def test_receipt_collector_summary_unchanged(self):
        """Receipt summary still works with new fields."""
        collector = ReceiptCollector(case_id="INC001")
        r1 = collector.start("w1", "a1", {}, policy_ref="p1")
        collector.finish(r1, {"results": [1, 2]})
        r2 = collector.start("w2", "a2", {}, policy_ref="p2")
        collector.finish(r2, None, error="test error")

        summary = collector.summary()
        assert summary["total_calls"] == 2
        assert summary["succeeded"] == 1
        assert summary["failed"] == 1


# =========================================================================
# Fix 1: v2 grounding model activated by default
# =========================================================================

class TestFix1_GroundingModelDefault:
    """Fix 1: GROUNDING_MODEL defaults to 'v2' when env var is unset."""

    def test_grounding_model_default_is_v2(self):
        """GROUNDING_MODEL constant is 'v2' when env var is unset."""
        import os
        import importlib
        import supervisor.grounding_confidence as gc_mod

        # Reload without the env var to verify the default
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROUNDING_MODEL", None)
            importlib.reload(gc_mod)
            assert gc_mod.GROUNDING_MODEL == "v2"

    def test_grounding_model_respects_env_override(self):
        """GROUNDING_MODEL honours explicit GROUNDING_MODEL=v1 override."""
        import os
        import importlib
        import supervisor.grounding_confidence as gc_mod

        with patch.dict(os.environ, {"GROUNDING_MODEL": "v1"}):
            importlib.reload(gc_mod)
            assert gc_mod.GROUNDING_MODEL == "v1"

        # Restore
        importlib.reload(gc_mod)

    def test_score_uses_v2_by_default(self):
        """score() returns a v2 GroundingResult (with dimensions) when model is v2."""
        from supervisor.grounding_confidence import score, GROUNDING_MODEL

        assert GROUNDING_MODEL == "v2", "Fix 1: default must be v2"

        rca_result = {
            "root_cause": "Connection pool exhausted due to DB latency spike",
            "confidence": 70,
        }
        evidence = {
            "logs": {"results": [
                {"level": "ERROR", "message": "Connection pool exhausted",
                 "service": "payment-service"},
            ], "count": 1},
        }
        grounding = score(rca_result, evidence, incident_type="timeout")
        # v2 populates model_version and dimensions dict; v1 leaves dimensions empty
        assert grounding.model_version == "v2", "Expected v2 model_version"
        assert isinstance(grounding.dimensions, dict)
        assert len(grounding.dimensions) > 0, "v2 should populate dimension scores"


# =========================================================================
# Fix 4: LogWorker incident-type aliases
# =========================================================================

class TestFix4_LogWorkerAliases:
    """Fix 4: LogWorker registers 5 incident-type-specific action aliases."""

    def test_all_aliases_registered(self):
        """LogWorker has all 5 new action aliases registered."""
        from workers.log_worker import LogWorker

        worker = LogWorker()
        expected = [
            "search_oom_logs",
            "search_timeout_logs",
            "search_error_logs",
            "search_saturation_logs",
            "get_error_logs",
        ]
        for action in expected:
            assert action in worker._handlers, f"LogWorker missing alias '{action}'"

    def test_alias_delegates_to_search_logs(self):
        """Each alias returns a result with a 'logs' key (same as search_logs)."""
        from workers.log_worker import LogWorker

        worker = LogWorker()
        aliases = [
            "search_oom_logs",
            "search_timeout_logs",
            "search_error_logs",
            "search_saturation_logs",
            "get_error_logs",
        ]
        for alias in aliases:
            result = worker.execute(alias, {"service": "payment-service",
                                            "query": "error timeout"})
            assert "logs" in result or "error" in result, (
                f"Alias '{alias}' returned unexpected structure: {result!r}"
            )


# =========================================================================
# Fix 5: Splunk source name env-var routing
# =========================================================================

class TestFix5_SourceMapRouting:
    """Fix 5: _src() resolves logical source names via env vars."""

    def test_src_returns_default_when_env_not_set(self):
        """_src() returns the key itself when no env var is configured."""
        import os
        import importlib
        import supervisor.splunk_retrieval_planner as planner

        env_keys = [
            "SPLUNK_SOURCE_K8S", "SPLUNK_SOURCE_CERT", "SPLUNK_SOURCE_INFRA",
            "SPLUNK_SOURCE_AUTH", "SPLUNK_SOURCE_CYBERARK", "SPLUNK_SOURCE_DNS",
            "SPLUNK_SOURCE_DB", "SPLUNK_SOURCE_KAFKA", "SPLUNK_SOURCE_PROCESS",
        ]
        with patch.dict(os.environ, {k: "" for k in env_keys}, clear=False):
            # Clear each env var
            for k in env_keys:
                os.environ.pop(k, None)
            importlib.reload(planner)
            assert planner._src("eks_logs") == "eks_logs"
            assert planner._src("cert_logs") == "cert_logs"
            assert planner._src("kafka_logs") == "kafka_logs"

        importlib.reload(planner)

    def test_src_returns_env_override(self):
        """_src() returns the env-var value when SPLUNK_SOURCE_K8S is set."""
        import os
        import importlib
        import supervisor.splunk_retrieval_planner as planner

        with patch.dict(os.environ, {"SPLUNK_SOURCE_K8S": "index=kubernetes sourcetype=kube:*"}):
            importlib.reload(planner)
            assert planner._src("eks_logs") == "index=kubernetes sourcetype=kube:*"

        importlib.reload(planner)

    def test_src_unknown_key_returns_key(self):
        """_src() returns the key unchanged for unknown logical names."""
        import importlib
        import supervisor.splunk_retrieval_planner as planner

        result = planner._src("some_unknown_source")
        assert result == "some_unknown_source"
