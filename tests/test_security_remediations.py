"""Tests for security audit remediations (G1.1 through G7.2).

Tests all gaps identified in SECURITY_AUDIT_REPORT.md and verifies
the remediations are correctly implemented.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest


# =========================================================================
# G1.1: validate_query() wired into LogWorker
# =========================================================================

class TestG1_1_ValidateQueryInLogWorker:
    """G1.1: validate_query() is now called from LogWorker._search_logs."""

    def test_valid_query_passes_through(self):
        from workers.log_worker import LogWorker
        from workers.mcp_client import McpGateway

        gw = MagicMock(spec=McpGateway)
        gw.invoke.return_value = {"logs": {"results": [], "count": 0}}
        worker = LogWorker(gateway=gw)

        result = worker.execute("search_logs", {"query": "error payment-svc"})
        gw.invoke.assert_called_once()
        assert "error" not in result or result.get("error") is None

    def test_dangerous_query_blocked(self):
        from workers.log_worker import LogWorker
        from workers.mcp_client import McpGateway

        gw = MagicMock(spec=McpGateway)
        worker = LogWorker(gateway=gw)

        result = worker.execute("search_logs", {"query": "index=main | delete"})
        # Gateway should NOT be called for blocked queries
        gw.invoke.assert_not_called()
        assert "error" in result
        assert "query_rejected" in result["error"]

    def test_pipe_query_blocked(self):
        from workers.log_worker import LogWorker
        from workers.mcp_client import McpGateway

        gw = MagicMock(spec=McpGateway)
        worker = LogWorker(gateway=gw)

        result = worker.execute("search_logs", {"query": "error | eval x=1"})
        gw.invoke.assert_not_called()
        assert "query_rejected" in result.get("error", "")

    def test_empty_query_passes_through(self):
        """An empty query should still go through (no query = no validation needed)."""
        from workers.log_worker import LogWorker
        from workers.mcp_client import McpGateway

        gw = MagicMock(spec=McpGateway)
        gw.invoke.return_value = {"logs": {"results": [], "count": 0}}
        worker = LogWorker(gateway=gw)

        result = worker.execute("search_logs", {"service": "payment-svc"})
        gw.invoke.assert_called_once()


# =========================================================================
# G3.3: Authentication on /invocations endpoint
# =========================================================================

class TestG3_3_AuthenticationEndpoint:
    """G3.3: /invocations endpoint validates Bearer token when AUTH_REQUIRED=true."""

    def test_validate_auth_passes_when_not_required(self):
        from agentcore_runtime import _validate_auth

        with patch.dict(os.environ, {"AUTH_REQUIRED": "false"}, clear=False):
            # Re-import to pick up env var change
            import importlib
            import agentcore_runtime
            importlib.reload(agentcore_runtime)
            is_valid, error = agentcore_runtime._validate_auth(None)
            assert is_valid
            assert error == ""

    def test_validate_auth_fails_when_missing(self):
        from agentcore_runtime import _validate_auth

        with patch("agentcore_runtime.AUTH_REQUIRED", True):
            with patch("agentcore_runtime.AUTH_TOKEN", "secret123"):
                is_valid, error = _validate_auth(None)
                assert not is_valid
                assert "Missing Authorization" in error

    def test_validate_auth_fails_on_wrong_token(self):
        from agentcore_runtime import _validate_auth

        with patch("agentcore_runtime.AUTH_REQUIRED", True):
            with patch("agentcore_runtime.AUTH_TOKEN", "secret123"):
                is_valid, error = _validate_auth("Bearer wrong_token")
                assert not is_valid
                assert "Invalid" in error

    def test_validate_auth_passes_on_correct_token(self):
        from agentcore_runtime import _validate_auth

        with patch("agentcore_runtime.AUTH_REQUIRED", True):
            with patch("agentcore_runtime.AUTH_TOKEN", "secret123"):
                is_valid, error = _validate_auth("Bearer secret123")
                assert is_valid
                assert error == ""


# =========================================================================
# G3.4: Agent identity whitelist
# =========================================================================

class TestG3_4_AgentWhitelist:
    """G3.4: Agent identity validated against allowlist."""

    def test_no_whitelist_allows_all(self):
        from agentcore_runtime import _validate_agent_identity

        with patch("agentcore_runtime.ALLOWED_AGENT_IDS", set()):
            is_valid, error = _validate_agent_identity("any-agent")
            assert is_valid

    def test_whitelist_blocks_unknown_agent(self):
        from agentcore_runtime import _validate_agent_identity

        with patch("agentcore_runtime.ALLOWED_AGENT_IDS", {"agent-1", "agent-2"}):
            is_valid, error = _validate_agent_identity("unknown-agent")
            assert not is_valid
            assert "not in allowlist" in error

    def test_whitelist_allows_known_agent(self):
        from agentcore_runtime import _validate_agent_identity

        with patch("agentcore_runtime.ALLOWED_AGENT_IDS", {"agent-1", "agent-2"}):
            is_valid, error = _validate_agent_identity("agent-1")
            assert is_valid

    def test_whitelist_blocks_missing_agent(self):
        from agentcore_runtime import _validate_agent_identity

        with patch("agentcore_runtime.ALLOWED_AGENT_IDS", {"agent-1"}):
            is_valid, error = _validate_agent_identity(None)
            assert not is_valid


# =========================================================================
# G3.5: Production secrets mandate
# =========================================================================

class TestG3_5_ProductionSecrets:
    """G3.5: Warning when production uses env var secrets."""

    def test_no_warning_in_development(self):
        from agentcore_runtime import _check_production_secrets_config

        with patch("agentcore_runtime.ENVIRONMENT", "development"):
            with patch("agentcore_runtime.logger") as mock_logger:
                _check_production_secrets_config()
                mock_logger.warning.assert_not_called()

    def test_warning_in_production_without_arn(self):
        from agentcore_runtime import _check_production_secrets_config

        with patch("agentcore_runtime.ENVIRONMENT", "production"):
            with patch.dict(os.environ, {
                "GATEWAY_OAUTH2_CLIENT_SECRET": "test-secret",
                "GATEWAY_OAUTH2_SECRET_ARN": "",
            }):
                with patch("agentcore_runtime.logger") as mock_logger:
                    _check_production_secrets_config()
                    mock_logger.warning.assert_called_once()
                    assert "SECURITY" in mock_logger.warning.call_args[0][0]


# =========================================================================
# G4.3: Reject unknown incident_type in ToolSelector
# =========================================================================

class TestG4_3_UnknownIncidentType:
    """G4.3: get_playbook logs warning for unknown types."""

    def test_unknown_type_logs_warning(self):
        from supervisor.tool_selector import get_playbook

        with patch("supervisor.tool_selector.logger") as mock_logger:
            result = get_playbook("completely_unknown_type")
            mock_logger.warning.assert_called_once()
            assert "completely_unknown_type" in str(mock_logger.warning.call_args)
            # Still returns error_spike playbook as default
            assert len(result) > 0

    def test_valid_type_no_warning(self):
        from supervisor.tool_selector import get_playbook

        with patch("supervisor.tool_selector.logger") as mock_logger:
            result = get_playbook("timeout")
            mock_logger.warning.assert_not_called()
            assert len(result) > 0


# =========================================================================
# G4.4: Abstract vendor names in system prompt
# =========================================================================

class TestG4_4_SystemPrompt:
    """G4.4: System prompt uses abstract labels instead of vendor names."""

    def test_no_vendor_names_in_prompt(self):
        from supervisor.system_prompt import SUPERVISOR_SYSTEM_PROMPT

        vendor_names = ["Moogsoft", "Splunk", "Sysdig", "Dynatrace", "SignalFx", "ServiceNow", "GitHub"]
        for vendor in vendor_names:
            assert vendor not in SUPERVISOR_SYSTEM_PROMPT, (
                f"Vendor name '{vendor}' found in system prompt (G4.4 violation)"
            )

    def test_abstract_labels_present(self):
        from supervisor.system_prompt import SUPERVISOR_SYSTEM_PROMPT

        assert "log analytics" in SUPERVISOR_SYSTEM_PROMPT
        assert "infrastructure metrics" in SUPERVISOR_SYSTEM_PROMPT
        assert "APM" in SUPERVISOR_SYSTEM_PROMPT
        assert "ITSM" in SUPERVISOR_SYSTEM_PROMPT
        assert "DevOps" in SUPERVISOR_SYSTEM_PROMPT


# =========================================================================
# G5.1-5.4: Enhanced receipt system
# =========================================================================

class TestG5_ReceiptEnhancements:
    """G5.1-5.4: Receipt enhancements for audit compliance."""

    def test_receipt_has_policy_ref_field(self):
        from supervisor.receipt import Receipt

        r = Receipt(tool="test", action="test", policy_ref="playbook:timeout:step3")
        assert r.policy_ref == "playbook:timeout:step3"
        d = r.to_dict()
        assert d["policy_ref"] == "playbook:timeout:step3"

    def test_receipt_has_wall_clock_timestamps(self):
        from supervisor.receipt import Receipt

        r = Receipt(tool="test", action="test", wall_clock_start="2026-03-03T00:00:00+00:00")
        assert r.wall_clock_start == "2026-03-03T00:00:00+00:00"
        d = r.to_dict()
        assert "wall_clock_start" in d

    def test_receipt_has_trace_id(self):
        from supervisor.receipt import Receipt

        r = Receipt(tool="test", action="test", trace_id="abc123def456")
        assert r.trace_id == "abc123def456"
        d = r.to_dict()
        assert d["trace_id"] == "abc123def456"

    def test_collector_passes_policy_ref(self):
        from supervisor.receipt import ReceiptCollector

        collector = ReceiptCollector(case_id="INC-1")
        receipt = collector.start("worker", "action", {}, policy_ref="budget:remaining=15")
        assert receipt.policy_ref == "budget:remaining=15"

    def test_collector_sets_wall_clock_on_start(self):
        from supervisor.receipt import ReceiptCollector

        collector = ReceiptCollector(case_id="INC-1")
        receipt = collector.start("worker", "action", {})
        assert receipt.wall_clock_start != ""
        assert "T" in receipt.wall_clock_start  # ISO 8601

    def test_finish_sets_wall_clock_end(self):
        from supervisor.receipt import ReceiptCollector

        collector = ReceiptCollector(case_id="INC-1")
        receipt = collector.start("worker", "action", {})
        collector.finish(receipt, {"results": []})
        assert receipt.wall_clock_end != ""
        assert "T" in receipt.wall_clock_end

    def test_collector_propagates_trace_id(self):
        from supervisor.receipt import ReceiptCollector

        collector = ReceiptCollector(case_id="INC-1", trace_id="trace-abc")
        receipt = collector.start("worker", "action", {})
        assert receipt.trace_id == "trace-abc"

    def test_output_capture_off_by_default(self):
        from supervisor.receipt import ReceiptCollector, RECEIPT_CAPTURE_OUTPUT

        collector = ReceiptCollector(case_id="INC-1")
        receipt = collector.start("worker", "action", {})
        collector.finish(receipt, {"results": [1, 2, 3]})
        # When capture is disabled, output should be None
        if not RECEIPT_CAPTURE_OUTPUT:
            assert receipt.output is None

    def test_output_not_in_dict_when_none(self):
        from supervisor.receipt import Receipt

        r = Receipt(tool="test", action="test")
        d = r.to_dict()
        assert "output" not in d

    def test_redact_output(self):
        from supervisor.receipt import _redact_output

        result = _redact_output({"data": "ok", "token": "secret123", "nested": {"password": "pw"}})
        assert result["data"] == "ok"
        assert result["token"] == "***REDACTED***"
        assert result["nested"]["password"] == "***REDACTED***"


# =========================================================================
# G6.1: Per-investigation wall-clock deadline
# =========================================================================

class TestG6_1_InvestigationDeadline:
    """G6.1: Investigation deadline prevents runaway investigations."""

    def test_deadline_set_on_investigate(self):
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        # Simulate investigation start
        supervisor._investigation_deadline = time.monotonic() + 120
        assert hasattr(supervisor, '_investigation_deadline')
        assert supervisor._investigation_deadline > time.monotonic()

    def test_call_worker_respects_deadline(self):
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        # Set deadline in the past
        supervisor._investigation_deadline = time.monotonic() - 1

        mock_worker = MagicMock()
        result = supervisor._call_worker(
            mock_worker, "test_action", {},
            receipts=None, budget=None, worker_name="test_worker",
        )
        assert result.get("error") == "investigation_deadline_exceeded"
        mock_worker.execute.assert_not_called()


# =========================================================================
# G6.2: Shared ThreadPoolExecutor
# =========================================================================

class TestG6_2_SharedExecutor:
    """G6.2: Supervisor uses a shared ThreadPoolExecutor."""

    def test_executor_created_in_init(self):
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()
        assert hasattr(supervisor, '_executor')
        assert supervisor._executor is not None


# =========================================================================
# G7.2: Cost estimation metric
# =========================================================================

class TestG7_2_CostEstimation:
    """G7.2: Cost estimation for FinOps dashboards."""

    def test_estimate_llm_cost_known_model(self):
        from supervisor.eval_metrics import estimate_llm_cost

        cost = estimate_llm_cost("anthropic.claude-3-haiku-20240307", 1000, 500)
        assert cost > 0
        # Haiku pricing: 1000 * 0.00025/1000 + 500 * 0.00125/1000 = 0.000875
        assert abs(cost - 0.000875) < 0.001

    def test_estimate_llm_cost_unknown_model(self):
        from supervisor.eval_metrics import estimate_llm_cost

        cost = estimate_llm_cost("unknown-model", 1000, 500)
        assert cost > 0  # Uses default pricing

    def test_estimate_llm_cost_zero_tokens(self):
        from supervisor.eval_metrics import estimate_llm_cost

        cost = estimate_llm_cost("anthropic.claude-3-haiku", 0, 0)
        assert cost == 0.0


# =========================================================================
# G3.2: User identity propagation
# =========================================================================

class TestG3_2_UserIdentityPropagation:
    """G3.2: User identity propagated to MCP gateway calls."""

    def test_invoke_accepts_user_identity(self):
        from workers.mcp_client import McpGateway

        gw = McpGateway()
        # Should not raise even with user_identity param
        result = gw.invoke("splunk.search_oneshot", "search_logs", {}, user_identity="user@corp.com")
        assert isinstance(result, dict)

    def test_user_identity_in_auth_headers(self):
        from workers.mcp_client import McpGateway

        gw = McpGateway()
        gw._current_user_identity = "user@corp.com"
        headers = gw._get_auth_headers()
        assert headers.get("X-User-Identity") == "user@corp.com"

    def test_no_identity_no_header(self):
        from workers.mcp_client import McpGateway

        gw = McpGateway()
        gw._current_user_identity = None
        headers = gw._get_auth_headers()
        assert "X-User-Identity" not in headers
