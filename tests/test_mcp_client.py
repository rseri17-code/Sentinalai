"""Tests for the AgentCore MCP gateway client."""

import json
import pytest
from unittest.mock import patch, MagicMock

from workers.mcp_client import (
    get_server_for_tool,
    get_arn_for_tool,
    invoke_mcp_tool,
    _stub_response,
    _parse_agent_response,
    _to_gateway_tool_name,
    McpGateway,
    dispose,
)

try:
    from botocore.exceptions import ClientError
    _HAS_BOTOCORE = True
except ImportError:
    _HAS_BOTOCORE = False


class TestToolToServerMapping:
    """Tests for MCP tool name to server mapping."""

    def test_moogsoft_tools_map_correctly(self):
        assert get_server_for_tool("moogsoft.get_incident_by_id") == "moogsoft"
        assert get_server_for_tool("moogsoft.get_incidents") == "moogsoft"
        assert get_server_for_tool("moogsoft.get_alerts") == "moogsoft"

    def test_splunk_tools_map_correctly(self):
        assert get_server_for_tool("splunk.search_oneshot") == "splunk"
        assert get_server_for_tool("splunk.get_change_data") == "splunk"
        assert get_server_for_tool("splunk.get_host_metrics") == "splunk"

    def test_sysdig_tools_map_correctly(self):
        assert get_server_for_tool("sysdig.query_metrics") == "sysdig"
        assert get_server_for_tool("sysdig.golden_signals") == "sysdig"
        assert get_server_for_tool("sysdig.get_events") == "sysdig"

    def test_signalfx_tools_map_correctly(self):
        assert get_server_for_tool("signalfx.query_signalfx_metrics") == "signalfx"

    def test_servicenow_tools_map_correctly(self):
        assert get_server_for_tool("servicenow.get_ci_details") == "servicenow"
        assert get_server_for_tool("servicenow.get_change_records") == "servicenow"

    def test_github_tools_map_correctly(self):
        assert get_server_for_tool("github.get_pr_details") == "github"
        assert get_server_for_tool("github.get_workflow_runs") == "github"

    def test_unknown_tool_returns_empty(self):
        assert get_server_for_tool("unknown.tool") == ""
        assert get_server_for_tool("") == ""


class TestGetArnForTool:
    """Tests for ARN resolution from tool names."""

    def test_no_arn_when_env_not_set(self):
        """Without MCP_*_TOOL_ARN env vars, all ARNs should be empty."""
        assert get_arn_for_tool("moogsoft.get_incident_by_id") == ""
        assert get_arn_for_tool("splunk.search_oneshot") == ""

    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:aws:test:moogsoft"})
    def test_arn_resolved_when_configured(self):
        assert get_arn_for_tool("moogsoft.get_incident_by_id") == "arn:aws:test:moogsoft"

    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"splunk": "arn:aws:test:splunk"})
    def test_arn_not_cross_resolved(self):
        """Moogsoft tool should not get Splunk ARN."""
        assert get_arn_for_tool("moogsoft.get_incident_by_id") == ""


class TestGatewayToolNameMapping:
    """Tests for internal -> AgentCore gateway tool name conversion."""

    def test_splunk_mapping(self):
        assert _to_gateway_tool_name("splunk.search_oneshot") == "SplunkTarget___search_oneshot"

    def test_moogsoft_mapping(self):
        assert _to_gateway_tool_name("moogsoft.get_incident_by_id") == "MoogsoftTarget___get_incident_by_id"

    def test_servicenow_mapping(self):
        assert _to_gateway_tool_name("servicenow.get_ci_details") == "ServiceNowTarget___get_ci_details"

    def test_github_mapping(self):
        assert _to_gateway_tool_name("github.get_pr_details") == "GitHubTarget___get_pr_details"

    def test_dynatrace_mapping(self):
        assert _to_gateway_tool_name("dynatrace.get_problems") == "DynatraceTarget___get_problems"

    def test_unknown_tool_returns_unchanged(self):
        assert _to_gateway_tool_name("unknown.tool") == "unknown.tool"


class TestStubResponses:
    """Tests for stub/fallback responses when gateway not configured."""

    def test_moogsoft_stub_has_incident(self):
        result = _stub_response("moogsoft.get_incident_by_id", "get_incident_by_id", {"incident_id": "INC001"})
        assert "incident" in result
        assert result["incident"]["incident_id"] == "INC001"

    def test_splunk_stub_has_logs(self):
        result = _stub_response("splunk.search_oneshot", "search_logs", {})
        assert "logs" in result
        assert isinstance(result["logs"]["results"], list)

    def test_splunk_change_stub(self):
        result = _stub_response("splunk.get_change_data", "get_change_data", {})
        assert "changes" in result

    def test_sysdig_metrics_stub(self):
        result = _stub_response("sysdig.query_metrics", "query_metrics", {})
        assert "metrics" in result

    def test_sysdig_events_stub(self):
        result = _stub_response("sysdig.get_events", "get_events", {})
        assert "events" in result

    def test_sysdig_golden_signals_stub(self):
        result = _stub_response("sysdig.golden_signals", "get_golden_signals", {})
        assert "signals" in result

    def test_signalfx_stub(self):
        result = _stub_response("signalfx.query_signalfx_metrics", "query_metrics", {})
        assert "metrics" in result

    def test_servicenow_ci_stub(self):
        result = _stub_response("servicenow.get_ci_details", "get_ci_details", {})
        assert "ci" in result

    def test_servicenow_incidents_stub(self):
        result = _stub_response("servicenow.search_incidents", "search_incidents", {})
        assert "incidents" in result

    def test_servicenow_change_records_stub(self):
        result = _stub_response("servicenow.get_change_records", "get_change_records", {})
        assert "change_records" in result

    def test_servicenow_known_errors_stub(self):
        result = _stub_response("servicenow.get_known_errors", "get_known_errors", {})
        assert "known_errors" in result

    def test_github_deployments_stub(self):
        result = _stub_response("github.get_recent_deployments", "get_recent_deployments", {})
        assert "deployments" in result

    def test_github_pr_stub(self):
        result = _stub_response("github.get_pr_details", "get_pr_details", {})
        assert "pr" in result

    def test_github_commit_stub(self):
        result = _stub_response("github.get_commit_diff", "get_commit_diff", {})
        assert "commit" in result

    def test_github_workflow_stub(self):
        result = _stub_response("github.get_workflow_runs", "get_workflow_runs", {})
        assert "workflow_runs" in result

    def test_unknown_stub_returns_empty(self):
        result = _stub_response("unknown.tool", "action", {})
        assert result == {}


class TestInvokeMcpTool:
    """Tests for the core invoke_mcp_tool function (routes through McpGateway)."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_returns_stub_when_no_arn(self):
        """When no ARN is configured, should return stub response."""
        result = invoke_mcp_tool("moogsoft.get_incident_by_id", "get_incident_by_id", {"incident_id": "INC001"})
        assert isinstance(result, dict)
        assert "incident" in result

    def test_returns_stub_when_no_client(self):
        """When boto3 client creation fails, should return stub."""
        gw = McpGateway.get_instance()
        with patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:test"}):
            with patch.object(gw, "_get_boto3_client", return_value=None):
                result = invoke_mcp_tool(
                    "moogsoft.get_incident_by_id", "get_incident_by_id",
                    {"incident_id": "INC001"},
                )
                assert isinstance(result, dict)

    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:aws:test:moogsoft"})
    def test_calls_invoke_inline_agent(self):
        """When ARN is set and client is available, should call invoke_inline_agent."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.return_value = {
            "completion": [{"chunk": {"bytes": json.dumps({"incident": {"id": "INC001"}}).encode()}}],
        }
        gw = McpGateway.get_instance()
        gw._boto3_client = mock_client

        result = invoke_mcp_tool(
            "moogsoft.get_incident_by_id", "get_incident_by_id",
            {"incident_id": "INC001"},
        )
        mock_client.invoke_inline_agent.assert_called_once()
        assert "incident" in result

    @pytest.mark.skipif(not _HAS_BOTOCORE, reason="botocore not installed")
    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:aws:test:moogsoft"})
    def test_handles_client_error(self):
        """ClientError should return error dict, not raise."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeInlineAgent",
        )
        gw = McpGateway.get_instance()
        gw._boto3_client = mock_client

        result = invoke_mcp_tool(
            "moogsoft.get_incident_by_id", "get_incident_by_id",
            {"incident_id": "INC001"},
        )
        assert "error" in result
        assert "ThrottlingException" in result["error"]

    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"splunk": "arn:aws:test:splunk"})
    def test_handles_generic_exception(self):
        """Generic exceptions should return error dict."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = RuntimeError("Connection reset")
        gw = McpGateway.get_instance()
        gw._boto3_client = mock_client

        result = invoke_mcp_tool(
            "splunk.search_oneshot", "search_logs", {},
        )
        assert "error" in result
        assert "Connection reset" in result["error"]


class TestParseAgentResponse:
    """Tests for parsing bedrock-agent-runtime responses."""

    def test_parses_json_response(self):
        response = {
            "completion": [
                {"chunk": {"bytes": b'{"data": "test"}'}},
            ],
        }
        result = _parse_agent_response(response)
        assert result == {"data": "test"}

    def test_concatenates_multiple_chunks(self):
        response = {
            "completion": [
                {"chunk": {"bytes": b'{"da'}},
                {"chunk": {"bytes": b'ta": "test"}'}},
            ],
        }
        result = _parse_agent_response(response)
        assert result == {"data": "test"}

    def test_returns_raw_for_non_json(self):
        response = {
            "completion": [
                {"chunk": {"bytes": b"plain text response"}},
            ],
        }
        result = _parse_agent_response(response)
        assert result["raw_response"] == "plain text response"

    def test_empty_completion_returns_empty(self):
        response = {"completion": []}
        result = _parse_agent_response(response)
        assert result["raw_response"] == "empty"

    def test_string_completion(self):
        response = {"completion": "direct string"}
        result = _parse_agent_response(response)
        assert result == {"raw_response": "direct string"}


class TestDispose:
    """Tests for client cleanup."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_dispose_resets_client(self):
        import workers.mcp_client as mc
        mc._client = "something"
        dispose()
        assert mc._client is None

    def test_dispose_clears_gateway(self):
        gw = McpGateway.get_instance()
        gw._boto3_client = MagicMock()
        dispose()
        assert gw._boto3_client is None
