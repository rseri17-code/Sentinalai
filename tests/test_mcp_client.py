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


class TestStubResponses:
    """Tests for stub/fallback responses when ARNs not configured."""

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

    def test_unknown_stub_returns_empty(self):
        result = _stub_response("unknown.tool", "action", {})
        assert result == {}


class TestInvokeMcpTool:
    """Tests for the core invoke_mcp_tool function."""

    def test_returns_stub_when_no_arn(self):
        """When no ARN is configured, should return stub response."""
        result = invoke_mcp_tool("moogsoft.get_incident_by_id", "get_incident_by_id", {"incident_id": "INC001"})
        assert isinstance(result, dict)
        assert "incident" in result

    def test_returns_stub_when_no_client(self):
        """When client creation fails, should return stub."""
        with patch("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:test"}):
            with patch("workers.mcp_client._get_client", return_value=None):
                result = invoke_mcp_tool(
                    "moogsoft.get_incident_by_id", "get_incident_by_id",
                    {"incident_id": "INC001"},
                )
                assert isinstance(result, dict)

    @patch("workers.mcp_client._get_client")
    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:aws:test:moogsoft"})
    def test_calls_invoke_inline_agent(self, mock_get_client):
        """When ARN is set and client is available, should call invoke_inline_agent."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.return_value = {
            "completion": [{"chunk": {"bytes": json.dumps({"incident": {"id": "INC001"}}).encode()}}],
        }
        mock_get_client.return_value = mock_client

        result = invoke_mcp_tool(
            "moogsoft.get_incident_by_id", "get_incident_by_id",
            {"incident_id": "INC001"},
        )
        mock_client.invoke_inline_agent.assert_called_once()
        assert "incident" in result

    @pytest.mark.skipif(not _HAS_BOTOCORE, reason="botocore not installed")
    @patch("workers.mcp_client._get_client")
    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"moogsoft": "arn:aws:test:moogsoft"})
    def test_handles_client_error(self, mock_get_client):
        """ClientError should return error dict, not raise."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeInlineAgent",
        )
        mock_get_client.return_value = mock_client

        result = invoke_mcp_tool(
            "moogsoft.get_incident_by_id", "get_incident_by_id",
            {"incident_id": "INC001"},
        )
        assert "error" in result
        assert "ThrottlingException" in result["error"]

    @patch("workers.mcp_client._get_client")
    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"splunk": "arn:aws:test:splunk"})
    def test_handles_generic_exception(self, mock_get_client):
        """Generic exceptions should return error dict."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = RuntimeError("Connection reset")
        mock_get_client.return_value = mock_client

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

    def test_dispose_resets_client(self):
        import workers.mcp_client as mc
        mc._client = "something"
        dispose()
        assert mc._client is None
