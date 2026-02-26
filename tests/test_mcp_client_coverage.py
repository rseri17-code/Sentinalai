"""Extended tests for workers/mcp_client.py — covering additional code paths.

Covers:
- _has_any_arn helper
- _get_client lazy init paths
- _parse_agent_response iterator path
- invoke_mcp_tool with generic exceptions (non-ClientError)
- Sysdig stub response branches (signal/golden keywords)
"""

import json
import pytest
from unittest.mock import patch, MagicMock

import workers.mcp_client as mc
from workers.mcp_client import (
    invoke_mcp_tool,
    _stub_response,
    _parse_agent_response,
    _has_any_arn,
    get_server_for_tool,
    get_arn_for_tool,
    dispose,
)


class TestHasAnyArn:
    """Tests for _has_any_arn helper."""

    def test_false_when_no_arns(self):
        with patch.dict(mc.MCP_TOOL_ARNS, {"moogsoft": "", "splunk": "", "sysdig": "", "signalfx": ""}):
            assert _has_any_arn() is False

    def test_true_when_one_arn_set(self):
        with patch.dict(mc.MCP_TOOL_ARNS, {"moogsoft": "arn:aws:test", "splunk": "", "sysdig": "", "signalfx": ""}):
            assert _has_any_arn() is True


class TestGetClientPaths:
    """Tests for _get_client lazy initialization."""

    def teardown_method(self):
        mc._client = None

    def test_returns_cached_client(self):
        sentinel = MagicMock()
        mc._client = sentinel
        result = mc._get_client()
        assert result is sentinel

    def test_returns_none_when_boto3_unavailable(self):
        mc._client = None
        with patch.object(mc, "_BOTO3_AVAILABLE", False):
            result = mc._get_client()
        assert result is None

    def test_creates_client_when_boto3_available(self):
        mc._client = None
        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        with patch.object(mc, "_BOTO3_AVAILABLE", True):
            original_boto3 = getattr(mc, "boto3", None)
            original_config = getattr(mc, "BotoConfig", None)
            try:
                mc.boto3 = mock_boto3
                mc.BotoConfig = MagicMock()
                result = mc._get_client()
            finally:
                if original_boto3 is None:
                    delattr(mc, "boto3") if hasattr(mc, "boto3") else None
                else:
                    mc.boto3 = original_boto3
                if original_config is None:
                    delattr(mc, "BotoConfig") if hasattr(mc, "BotoConfig") else None
                else:
                    mc.BotoConfig = original_config
        assert result is mock_client

    def test_handles_client_creation_error(self):
        mc._client = None
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = RuntimeError("AWS error")
        with patch.object(mc, "_BOTO3_AVAILABLE", True):
            original_boto3 = getattr(mc, "boto3", None)
            original_config = getattr(mc, "BotoConfig", None)
            try:
                mc.boto3 = mock_boto3
                mc.BotoConfig = MagicMock()
                result = mc._get_client()
            finally:
                if original_boto3 is None:
                    delattr(mc, "boto3") if hasattr(mc, "boto3") else None
                else:
                    mc.boto3 = original_boto3
                if original_config is None:
                    delattr(mc, "BotoConfig") if hasattr(mc, "BotoConfig") else None
                else:
                    mc.BotoConfig = original_config
        assert result is None


class TestParseAgentResponseExtended:
    """Extended tests for _parse_agent_response."""

    def test_handles_iterator_completion(self):
        """Test the hasattr(__iter__) path for streaming responses."""

        class StreamingCompletion:
            def __iter__(self):
                yield {"chunk": {"bytes": b'{"data": '}}
                yield {"chunk": {"bytes": b'"streamed"}'}}

        response = {"completion": StreamingCompletion()}
        result = _parse_agent_response(response)
        assert result == {"data": "streamed"}

    def test_handles_iterator_non_json(self):
        """Iterator returning non-JSON text."""

        class StreamingCompletion:
            def __iter__(self):
                yield {"chunk": {"bytes": b"plain text"}}

        response = {"completion": StreamingCompletion()}
        result = _parse_agent_response(response)
        assert result["raw_response"] == "plain text"

    def test_handles_empty_dict_events_in_list(self):
        response = {"completion": [{}]}
        result = _parse_agent_response(response)
        assert result["raw_response"] == "empty"

    def test_handles_missing_completion_key(self):
        response = {}
        result = _parse_agent_response(response)
        assert "raw_response" in result


class TestStubResponseExtended:
    """Extended stub response tests for branch coverage."""

    def test_sysdig_signal_keyword(self):
        """Sysdig stub with 'signal' in action."""
        result = _stub_response("sysdig.golden_signals", "get_golden_signals", {})
        assert "signals" in result

    def test_sysdig_golden_keyword(self):
        """Sysdig stub with 'golden' in action."""
        result = _stub_response("sysdig.golden_signals", "golden_metrics", {})
        assert "signals" in result

    def test_sysdig_discover_resources(self):
        """Sysdig stub for non-event/non-signal actions."""
        result = _stub_response("sysdig.discover_resources", "discover", {})
        assert "metrics" in result

    def test_moogsoft_stub_default_id(self):
        """Moogsoft stub with no incident_id defaults to 'unknown'."""
        result = _stub_response("moogsoft.get_incident_by_id", "get", {})
        assert result["incident"]["incident_id"] == "unknown"


class TestInvokeMcpToolExtended:
    """Extended invoke tests for error handling branches."""

    @patch("workers.mcp_client._get_client")
    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"splunk": "arn:aws:test:splunk"})
    def test_invoke_with_session_id_in_params(self, mock_get_client):
        """Session ID from params is forwarded to invoke_inline_agent."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.return_value = {
            "completion": [{"chunk": {"bytes": b'{"ok": true}'}}],
        }
        mock_get_client.return_value = mock_client

        result = invoke_mcp_tool(
            "splunk.search_oneshot", "search",
            {"session_id": "custom-session-123"},
        )
        call_kwargs = mock_client.invoke_inline_agent.call_args
        assert call_kwargs[1]["sessionId"] == "custom-session-123"

    @patch("workers.mcp_client._get_client")
    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"sysdig": "arn:aws:test:sysdig"})
    def test_invoke_generic_exception_returns_error_dict(self, mock_get_client):
        """Non-ClientError exceptions return error dict with message."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = ConnectionError("DNS failure")
        mock_get_client.return_value = mock_client

        result = invoke_mcp_tool("sysdig.query_metrics", "query", {})
        assert "error" in result
        assert "DNS failure" in result["error"]
        assert result["tool"] == "sysdig.query_metrics"
