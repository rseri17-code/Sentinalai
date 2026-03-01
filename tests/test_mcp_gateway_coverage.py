"""
Phase 2 coverage tests for workers/mcp_client.py.

Targets uncovered paths: gateway invocation, MCPClient creation,
legacy invocation, OAuth2 secrets manager, and stub responses.
"""

import json
import time
import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock

from workers.mcp_client import (
    McpGateway,
    _stub_response,
    _to_gateway_tool_name,
    _fetch_secret_from_asm,
    get_server_for_tool,
    get_arn_for_tool,
    _get_client,
)


# =========================================================================
# _to_gateway_tool_name
# =========================================================================

class TestGatewayToolNameConversion:
    """Cover _to_gateway_tool_name helper."""

    def test_known_tool_conversion(self):
        result = _to_gateway_tool_name("splunk.search_oneshot")
        assert "SplunkTarget" in result
        assert "search_oneshot" in result

    def test_unknown_tool_returns_as_is(self):
        result = _to_gateway_tool_name("unknown.tool")
        assert result == "unknown.tool"


# =========================================================================
# _fetch_secret_from_asm
# =========================================================================

class TestFetchSecretFromASM:
    """Cover _fetch_secret_from_asm function."""

    def _patch_boto3(self, get_secret_return):
        """Helper to inject a mock boto3 into the mcp_client module."""
        import workers.mcp_client as mod
        mock_boto3 = MagicMock()
        sm_client = MagicMock()
        sm_client.get_secret_value.return_value = get_secret_return
        mock_boto3.client.return_value = sm_client
        return mock_boto3, sm_client

    def test_plain_string_secret(self):
        mock_boto3, _ = self._patch_boto3({"SecretString": "my-secret-value"})
        import workers.mcp_client as mod
        orig = getattr(mod, "boto3", None)
        try:
            mod.boto3 = mock_boto3
            with patch.object(mod, "_BOTO3_AVAILABLE", True):
                result = _fetch_secret_from_asm("arn:aws:secretsmanager:us-east-1:123:secret:test")
            assert result == "my-secret-value"
        finally:
            if orig is None:
                delattr(mod, "boto3") if hasattr(mod, "boto3") else None
            else:
                mod.boto3 = orig

    def test_json_secret_with_client_secret(self):
        secret_json = json.dumps({"client_secret": "json-secret"})
        mock_boto3, _ = self._patch_boto3({"SecretString": secret_json})
        import workers.mcp_client as mod
        orig = getattr(mod, "boto3", None)
        try:
            mod.boto3 = mock_boto3
            with patch.object(mod, "_BOTO3_AVAILABLE", True):
                result = _fetch_secret_from_asm("arn:aws:secretsmanager:us-east-1:123:secret:test")
            assert result == "json-secret"
        finally:
            if orig is None:
                delattr(mod, "boto3") if hasattr(mod, "boto3") else None
            else:
                mod.boto3 = orig

    def test_json_secret_with_secret_key(self):
        secret_json = json.dumps({"secret": "alt-secret"})
        mock_boto3, _ = self._patch_boto3({"SecretString": secret_json})
        import workers.mcp_client as mod
        orig = getattr(mod, "boto3", None)
        try:
            mod.boto3 = mock_boto3
            with patch.object(mod, "_BOTO3_AVAILABLE", True):
                result = _fetch_secret_from_asm("arn:aws:secretsmanager:us-east-1:123:secret:test")
            assert result == "alt-secret"
        finally:
            if orig is None:
                delattr(mod, "boto3") if hasattr(mod, "boto3") else None
            else:
                mod.boto3 = orig

    def test_json_parse_error_returns_raw(self):
        mock_boto3, _ = self._patch_boto3({"SecretString": "{invalid json"})
        import workers.mcp_client as mod
        orig = getattr(mod, "boto3", None)
        try:
            mod.boto3 = mock_boto3
            with patch.object(mod, "_BOTO3_AVAILABLE", True):
                result = _fetch_secret_from_asm("arn:aws:secretsmanager:us-east-1:123:secret:test")
            assert result == "{invalid json"
        finally:
            if orig is None:
                delattr(mod, "boto3") if hasattr(mod, "boto3") else None
            else:
                mod.boto3 = orig

    def test_exception_returns_empty(self):
        import workers.mcp_client as mod
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception("AWS error")
        orig = getattr(mod, "boto3", None)
        try:
            mod.boto3 = mock_boto3
            with patch.object(mod, "_BOTO3_AVAILABLE", True):
                result = _fetch_secret_from_asm("arn:aws:secretsmanager:us-east-1:123:secret:test")
            assert result == ""
        finally:
            if orig is None:
                delattr(mod, "boto3") if hasattr(mod, "boto3") else None
            else:
                mod.boto3 = orig

    @patch("workers.mcp_client._BOTO3_AVAILABLE", False)
    def test_boto3_not_available(self):
        result = _fetch_secret_from_asm("arn:aws:secretsmanager:us-east-1:123456:secret:test")
        assert result == ""


# =========================================================================
# Gateway invocation path
# =========================================================================

class TestGatewayInvocation:
    """Cover _invoke_via_gateway paths."""

    def test_gateway_call_returns_dict_directly(self):
        """When MCP client returns a dict, it's returned directly."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        mock_client = MagicMock()
        mock_client.call_tool_sync.return_value = {"results": [{"key": "value"}]}
        gw._mcp_client = mock_client

        with patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {"query": "test"})
        assert result == {"results": [{"key": "value"}]}

    def test_gateway_call_returns_tool_result_with_content(self):
        """When MCP client returns a ToolResult with content, parse it."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        # Create a mock ToolResult with content
        text_content = MagicMock()
        text_content.text = '{"results": [{"data": "parsed"}]}'
        tool_result = MagicMock()
        tool_result.content = [text_content]
        # Make isinstance check fail for dict but hasattr work for content
        mock_client = MagicMock()
        mock_client.call_tool_sync.return_value = tool_result

        gw._mcp_client = mock_client

        with patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {})
        assert result == {"results": [{"data": "parsed"}]}

    def test_gateway_call_returns_non_json_content(self):
        """When MCP client returns non-JSON text content, return raw_response."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        text_content = MagicMock()
        text_content.text = "not valid json"
        tool_result = MagicMock()
        tool_result.content = [text_content]
        mock_client = MagicMock()
        mock_client.call_tool_sync.return_value = tool_result
        gw._mcp_client = mock_client

        with patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {})
        assert "raw_response" in result

    def test_gateway_call_returns_unknown_type(self):
        """When MCP client returns an unknown type, wrap in raw_response."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        mock_client = MagicMock()
        # Return something that's not a dict and doesn't have .content
        result_obj = "plain string result"
        mock_client.call_tool_sync.return_value = result_obj
        gw._mcp_client = mock_client

        with patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {})
        assert "raw_response" in result

    def test_gateway_exception_returns_error(self):
        """When gateway raises, error dict returned."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        mock_client = MagicMock()
        mock_client.call_tool_sync.side_effect = Exception("Network error")
        gw._mcp_client = mock_client

        with patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {})
        assert "error" in result
        assert "gateway_exception" in result["error"]

    def test_gateway_401_retry_with_token_refresh(self):
        """On 401, invalidate OAuth2 token and retry once."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None

        mock_oauth2 = MagicMock()
        gw._oauth2_provider = mock_oauth2

        mock_client = MagicMock()
        # First call raises 401, second call (after retry) succeeds
        mock_client.call_tool_sync.side_effect = [
            Exception("401 Unauthorized"),
            {"results": [{"key": "retried"}]},
        ]
        gw._mcp_client = mock_client

        with patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {})

        mock_oauth2.invalidate.assert_called_once()

    def test_gateway_no_mcp_client_returns_stub(self):
        """When MCPClient creation returns None, return stub."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        with patch.object(gw, "_get_mcp_client", return_value=None), \
             patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            result = gw._invoke_via_gateway("splunk.search_oneshot", "search_oneshot", {})
        # Should get a stub response
        assert isinstance(result, dict)


# =========================================================================
# MCPClient creation
# =========================================================================

class TestMcpClientCreation:
    """Cover _get_mcp_client paths."""

    def test_cached_client_returned(self):
        """When _mcp_client is already set, return it."""
        gw = McpGateway.__new__(McpGateway)
        existing = MagicMock()
        gw._mcp_client = existing
        assert gw._get_mcp_client() is existing

    @patch("workers.mcp_client._MCP_SDK_AVAILABLE", False)
    def test_sdk_not_available(self):
        """When SDK not available, return None."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._oauth2_provider = None
        result = gw._get_mcp_client()
        assert result is None

    @patch("workers.mcp_client._MCP_SDK_AVAILABLE", True)
    @patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "")
    def test_no_gateway_url(self):
        """When gateway URL not set, return None."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._oauth2_provider = None
        result = gw._get_mcp_client()
        assert result is None

    @patch("workers.mcp_client._MCP_SDK_AVAILABLE", True)
    @patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", "https://gateway.test")
    def test_creation_exception_returns_none(self):
        """When MCPClient creation raises, return None."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._oauth2_provider = None
        with patch("workers.mcp_client.MCPClient", side_effect=Exception("Connection failed")):
            result = gw._get_mcp_client()
        assert result is None


# =========================================================================
# Legacy invocation
# =========================================================================

class TestLegacyInvocation:
    """Cover _invoke_via_legacy paths."""

    def test_legacy_success(self):
        """Successful legacy invoke_inline_agent call."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        mock_client = MagicMock()
        mock_client.invoke_inline_agent.return_value = {
            "completion": [{"chunk": {"bytes": b'{"result": "ok"}'}}],
        }
        gw._boto3_client = mock_client

        with patch("workers.mcp_client._parse_agent_response", return_value={"result": "ok"}):
            result = gw._invoke_via_legacy(
                "splunk.search_oneshot", "search_oneshot",
                {"query": "test"}, "arn:aws:bedrock:us-east-1:123:agent/test",
            )
        assert result == {"result": "ok"}

    def test_legacy_client_error(self):
        """When boto3 ClientError occurs, return error dict."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        mock_client = MagicMock()

        # Create a mock ClientError
        class MockClientError(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "ThrottlingException"}}

        mock_client.invoke_inline_agent.side_effect = MockClientError()
        gw._boto3_client = mock_client

        with patch("workers.mcp_client._BOTO3_AVAILABLE", True), \
             patch("workers.mcp_client._ClientError", MockClientError):
            result = gw._invoke_via_legacy(
                "splunk.search_oneshot", "search_oneshot",
                {}, "arn:aws:bedrock:us-east-1:123:agent/test",
            )
        assert "error" in result
        assert "ThrottlingException" in result["error"]

    def test_legacy_generic_exception(self):
        """When a non-ClientError occurs, return error dict."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = RuntimeError("Network failure")
        gw._boto3_client = mock_client

        result = gw._invoke_via_legacy(
            "splunk.search_oneshot", "search_oneshot",
            {}, "arn:aws:bedrock:us-east-1:123:agent/test",
        )
        assert "error" in result
        assert "mcp_exception" in result["error"]

    def test_legacy_no_client_returns_stub(self):
        """When boto3 client is None, return stub."""
        gw = McpGateway.__new__(McpGateway)
        gw._mcp_client = None
        gw._boto3_client = None
        gw._tools_cache = None
        gw._oauth2_provider = None

        with patch.object(gw, "_get_boto3_client", return_value=None):
            result = gw._invoke_via_legacy(
                "splunk.search_oneshot", "search_oneshot",
                {}, "arn:aws:bedrock:us-east-1:123:agent/test",
            )
        assert isinstance(result, dict)


# =========================================================================
# get_server_for_tool (static method on instance)
# =========================================================================

class TestStaticMethods:
    """Cover get_server_for_tool on the instance."""

    def test_instance_get_server_for_tool(self):
        assert McpGateway.get_server_for_tool("splunk.search_oneshot") == "splunk"
        assert McpGateway.get_server_for_tool("unknown.tool") == ""

    def test_module_level_get_client(self):
        """Module-level _get_client delegates to singleton."""
        with patch.object(McpGateway, "get_instance") as mock_inst:
            gw = MagicMock()
            gw._get_boto3_client.return_value = None
            mock_inst.return_value = gw
            result = _get_client()
            gw._get_boto3_client.assert_called_once()


# =========================================================================
# Stub responses for ServiceNow / GitHub
# =========================================================================

class TestStubResponses:
    """Cover _stub_response for ServiceNow and GitHub tool actions."""

    def test_servicenow_search_incidents(self):
        result = _stub_response("servicenow.search_incidents", "search_incidents", {})
        assert "incidents" in result

    def test_servicenow_get_ci_details(self):
        result = _stub_response("servicenow.get_ci_details", "get_ci_details", {})
        assert "ci" in result

    def test_servicenow_get_change_records(self):
        result = _stub_response("servicenow.get_change_records", "get_change_records", {})
        assert "change_records" in result

    def test_servicenow_get_known_errors(self):
        result = _stub_response("servicenow.get_known_errors", "get_known_errors", {})
        assert "known_errors" in result

    def test_servicenow_default(self):
        """When servicenow action doesn't match any pattern, return default."""
        # Use a known servicenow tool name so server resolves, but use
        # an action that doesn't match any patterns in the stub.
        result = _stub_response("servicenow.get_ci_details", "list_services", {})
        assert "ci" in result

    def test_github_get_deployments(self):
        result = _stub_response("github.get_recent_deployments", "get_recent_deployments", {})
        assert "deployments" in result

    def test_github_get_pr_details(self):
        result = _stub_response("github.get_pr_details", "get_pr_details", {})
        assert "pr" in result

    def test_github_get_commit_diff(self):
        result = _stub_response("github.get_commit_diff", "get_commit_diff", {})
        assert "commit" in result

    def test_github_get_workflow_runs(self):
        result = _stub_response("github.get_workflow_runs", "get_workflow_runs", {})
        assert "workflow_runs" in result

    def test_github_default(self):
        result = _stub_response("github.unknown_action", "unknown_action", {})
        assert isinstance(result, dict)

    def test_unknown_server(self):
        result = _stub_response("unknown.tool", "action", {})
        assert isinstance(result, dict)
