"""Extended tests for workers/mcp_client.py — covering additional code paths.

Covers:
- _has_any_arn helper
- McpGateway._get_boto3_client lazy init paths
- McpGateway singleton lifecycle
- _to_gateway_tool_name mapping (all servers)
- OAuth2CredentialProvider (client_credentials grant, token caching, refresh)
- McpGateway._get_auth_headers priority (OAuth2 -> static -> none)
- _parse_agent_response iterator path
- invoke_mcp_tool with generic exceptions (non-ClientError)
- Sysdig stub response branches (signal/golden keywords)
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import workers.mcp_client as mc
from workers.mcp_client import (
    invoke_mcp_tool,
    _stub_response,
    _parse_agent_response,
    _has_any_arn,
    _to_gateway_tool_name,
    get_server_for_tool,
    get_arn_for_tool,
    McpGateway,
    OAuth2CredentialProvider,
    RateLimiterRegistry,
    _TokenBucket,
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


class TestGetBoto3ClientPaths:
    """Tests for McpGateway._get_boto3_client lazy initialization."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_returns_cached_client(self):
        sentinel = MagicMock()
        gw = McpGateway.get_instance()
        gw._boto3_client = sentinel
        result = gw._get_boto3_client()
        assert result is sentinel

    def test_returns_none_when_boto3_unavailable(self):
        gw = McpGateway.get_instance()
        with patch.object(mc, "_BOTO3_AVAILABLE", False):
            result = gw._get_boto3_client()
        assert result is None

    def test_creates_client_when_boto3_available(self):
        gw = McpGateway.get_instance()
        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        with patch.object(mc, "_BOTO3_AVAILABLE", True):
            original_boto3 = getattr(mc, "boto3", None)
            original_config = getattr(mc, "BotoConfig", None)
            try:
                mc.boto3 = mock_boto3
                mc.BotoConfig = MagicMock()
                result = gw._get_boto3_client()
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
        gw = McpGateway.get_instance()
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = RuntimeError("AWS error")
        with patch.object(mc, "_BOTO3_AVAILABLE", True):
            original_boto3 = getattr(mc, "boto3", None)
            original_config = getattr(mc, "BotoConfig", None)
            try:
                mc.boto3 = mock_boto3
                mc.BotoConfig = MagicMock()
                result = gw._get_boto3_client()
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


class TestMcpGatewaySingleton:
    """Tests for McpGateway singleton lifecycle."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_get_instance_returns_same_object(self):
        gw1 = McpGateway.get_instance()
        gw2 = McpGateway.get_instance()
        assert gw1 is gw2

    def test_reset_clears_singleton(self):
        gw1 = McpGateway.get_instance()
        McpGateway.reset_instance()
        gw2 = McpGateway.get_instance()
        assert gw1 is not gw2

    def test_dispose_clears_all_clients(self):
        gw = McpGateway.get_instance()
        gw._boto3_client = MagicMock()
        gw._mcp_client = MagicMock()
        gw._tools_cache = {"tool": "data"}
        gw.dispose()
        assert gw._boto3_client is None
        assert gw._mcp_client is None
        assert gw._tools_cache is None

    def test_invoke_falls_through_to_stub(self):
        """With no gateway URL and no ARNs, invoke returns stub."""
        gw = McpGateway.get_instance()
        result = gw.invoke("moogsoft.get_incident_by_id", "get_incident_by_id", {"incident_id": "INC001"})
        assert isinstance(result, dict)
        assert "incident" in result


class TestGatewayToolNameMapping:
    """Tests for _to_gateway_tool_name conversion (all 7 servers)."""

    def test_all_servers_mapped(self):
        mappings = {
            "moogsoft.get_incident_by_id": "MoogsoftTarget___get_incident_by_id",
            "splunk.search_oneshot": "SplunkTarget___search_oneshot",
            "sysdig.query_metrics": "SysdigTarget___query_metrics",
            "signalfx.query_signalfx_metrics": "SignalFxTarget___query_signalfx_metrics",
            "dynatrace.get_problems": "DynatraceTarget___get_problems",
            "servicenow.get_ci_details": "ServiceNowTarget___get_ci_details",
            "github.get_pr_details": "GitHubTarget___get_pr_details",
        }
        for internal, expected in mappings.items():
            assert _to_gateway_tool_name(internal) == expected, f"Failed for {internal}"

    def test_unknown_tool_passes_through(self):
        assert _to_gateway_tool_name("unknown.tool") == "unknown.tool"

    def test_no_dot_passes_through(self):
        assert _to_gateway_tool_name("nodotname") == "nodotname"


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

    def test_dynatrace_problems_stub(self):
        result = _stub_response("dynatrace.get_problems", "get_problems", {})
        assert "problems" in result

    def test_dynatrace_events_stub(self):
        result = _stub_response("dynatrace.get_events", "get_events", {})
        assert "events" in result

    def test_dynatrace_metrics_stub(self):
        result = _stub_response("dynatrace.get_metrics", "get_metrics", {})
        assert "metrics" in result

    def test_signalfx_golden_signals_stub(self):
        result = _stub_response("signalfx.get_signalfx_active_incidents", "get_golden_signals", {})
        assert "signals" in result


class TestInvokeMcpToolExtended:
    """Extended invoke tests for error handling branches."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"splunk": "arn:aws:test:splunk"})
    def test_invoke_with_session_id_in_params(self):
        """Session ID from params is forwarded to invoke_inline_agent."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.return_value = {
            "completion": [{"chunk": {"bytes": b'{"ok": true}'}}],
        }
        gw = McpGateway.get_instance()
        gw._boto3_client = mock_client

        result = invoke_mcp_tool(
            "splunk.search_oneshot", "search",
            {"session_id": "custom-session-123"},
        )
        call_kwargs = mock_client.invoke_inline_agent.call_args
        assert call_kwargs[1]["sessionId"] == "custom-session-123"

    @patch.dict("workers.mcp_client.MCP_TOOL_ARNS", {"sysdig": "arn:aws:test:sysdig"})
    def test_invoke_generic_exception_returns_error_dict(self):
        """Non-ClientError exceptions return error dict with message."""
        mock_client = MagicMock()
        mock_client.invoke_inline_agent.side_effect = ConnectionError("DNS failure")
        gw = McpGateway.get_instance()
        gw._boto3_client = mock_client

        result = invoke_mcp_tool("sysdig.query_metrics", "query", {})
        assert "error" in result
        assert "DNS failure" in result["error"]
        assert result["tool"] == "sysdig.query_metrics"


class TestOAuth2CredentialProvider:
    """Tests for OAuth2 client_credentials grant with token caching."""

    def test_get_access_token_calls_token_endpoint(self):
        """Token endpoint is called with client_credentials grant."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "test-token-abc123",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = MagicMock()

        provider = OAuth2CredentialProvider(
            client_id="test-client-id",
            client_secret="test-secret",
            token_url="https://cognito.example.com/oauth2/token",
            scope="gateway/mcp.invoke",
        )

        with patch.object(mc, "_REQUESTS_AVAILABLE", True):
            with patch.object(mc, "_requests_lib") as mock_requests:
                mock_requests.post.return_value = mock_response
                token = provider.get_access_token()

        assert token == "test-token-abc123"
        call_kwargs = mock_requests.post.call_args
        assert call_kwargs[1]["data"]["grant_type"] == "client_credentials"
        assert call_kwargs[1]["data"]["client_id"] == "test-client-id"
        assert call_kwargs[1]["data"]["client_secret"] == "test-secret"
        assert call_kwargs[1]["data"]["scope"] == "gateway/mcp.invoke"

    def test_token_is_cached_on_second_call(self):
        """Second call returns cached token without hitting endpoint."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "cached-token",
            "expires_in": 3600,
        }
        mock_response.raise_for_status = MagicMock()

        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
            refresh_buffer_seconds=60,
        )

        with patch.object(mc, "_REQUESTS_AVAILABLE", True):
            with patch.object(mc, "_requests_lib") as mock_requests:
                mock_requests.post.return_value = mock_response
                token1 = provider.get_access_token()
                token2 = provider.get_access_token()

        assert token1 == "cached-token"
        assert token2 == "cached-token"
        # Only one HTTP call (cached on second)
        assert mock_requests.post.call_count == 1

    def test_token_refresh_when_expired(self):
        """Expired token triggers a new token request."""
        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
            refresh_buffer_seconds=60,
        )
        # Simulate an already-expired token
        provider._token = "old-token"
        provider._expiry = datetime.now(timezone.utc) - timedelta(minutes=5)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "refreshed-token",
            "expires_in": 7200,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(mc, "_REQUESTS_AVAILABLE", True):
            with patch.object(mc, "_requests_lib") as mock_requests:
                mock_requests.post.return_value = mock_response
                token = provider.get_access_token()

        assert token == "refreshed-token"
        assert mock_requests.post.call_count == 1

    def test_invalidate_clears_cached_token(self):
        """invalidate() forces refresh on next call."""
        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
        )
        provider._token = "valid-token"
        provider._expiry = datetime.now(timezone.utc) + timedelta(hours=1)

        provider.invalidate()
        assert provider._token is None
        assert provider._expiry is None

    def test_get_auth_headers_returns_bearer(self):
        """get_auth_headers wraps token in Authorization header."""
        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
        )
        provider._token = "my-token"
        provider._expiry = datetime.now(timezone.utc) + timedelta(hours=1)

        headers = provider.get_auth_headers()
        assert headers == {"Authorization": "Bearer my-token"}

    def test_returns_empty_when_requests_unavailable(self):
        """Without requests library, token refresh returns empty string."""
        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
        )
        with patch.object(mc, "_REQUESTS_AVAILABLE", False):
            token = provider.get_access_token()
        assert token == ""

    def test_returns_empty_on_http_error(self):
        """HTTP error from token endpoint returns empty string."""
        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
        )
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("401 Unauthorized")

        with patch.object(mc, "_REQUESTS_AVAILABLE", True):
            with patch.object(mc, "_requests_lib") as mock_requests:
                mock_requests.post.return_value = mock_response
                token = provider.get_access_token()

        assert token == ""

    def test_scope_omitted_when_empty(self):
        """When scope is empty, it is not included in the request."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "no-scope-token",
            "expires_in": 3600,
        }
        mock_response.raise_for_status = MagicMock()

        provider = OAuth2CredentialProvider(
            client_id="cid", client_secret="csec",
            token_url="https://example.com/token",
            scope="",
        )

        with patch.object(mc, "_REQUESTS_AVAILABLE", True):
            with patch.object(mc, "_requests_lib") as mock_requests:
                mock_requests.post.return_value = mock_response
                provider.get_access_token()

        data = mock_requests.post.call_args[1]["data"]
        assert "scope" not in data


class TestOAuth2FromEnv:
    """Tests for OAuth2CredentialProvider.from_env() factory."""

    def test_returns_none_when_no_client_id(self):
        """No provider created when GATEWAY_OAUTH2_CLIENT_ID is empty."""
        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", ""):
            result = OAuth2CredentialProvider.from_env()
        assert result is None

    def test_creates_provider_with_explicit_token_url(self):
        """Provider created with explicit token URL."""
        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", "my-client"):
            with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_SECRET", "my-secret"):
                with patch.object(mc, "GATEWAY_OAUTH2_TOKEN_URL", "https://auth.example.com/token"):
                    with patch.object(mc, "GATEWAY_OAUTH2_SCOPE", "gw/invoke"):
                        provider = OAuth2CredentialProvider.from_env()

        assert provider is not None
        assert provider._client_id == "my-client"
        assert provider._client_secret == "my-secret"
        assert provider._token_url == "https://auth.example.com/token"
        assert provider._scope == "gw/invoke"

    def test_derives_token_url_from_cognito_domain(self):
        """Token URL auto-derived from Cognito domain + region."""
        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", "my-client"):
            with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_SECRET", "my-secret"):
                with patch.object(mc, "GATEWAY_OAUTH2_TOKEN_URL", ""):
                    with patch.object(mc, "GATEWAY_COGNITO_DOMAIN", "my-pool"):
                        with patch.object(mc, "AWS_REGION", "eu-west-1"):
                            with patch.object(mc, "GATEWAY_OAUTH2_SCOPE", ""):
                                provider = OAuth2CredentialProvider.from_env()

        assert provider is not None
        assert provider._token_url == "https://my-pool.auth.eu-west-1.amazoncognito.com/oauth2/token"

    def test_returns_none_when_no_token_url(self):
        """No provider when neither token URL nor Cognito domain is set."""
        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", "my-client"):
            with patch.object(mc, "GATEWAY_OAUTH2_TOKEN_URL", ""):
                with patch.object(mc, "GATEWAY_COGNITO_DOMAIN", ""):
                    result = OAuth2CredentialProvider.from_env()
        assert result is None

    def test_returns_none_when_no_client_secret(self):
        """No provider when no client secret source is available."""
        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", "my-client"):
            with patch.object(mc, "GATEWAY_OAUTH2_TOKEN_URL", "https://auth.example.com/token"):
                with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_SECRET", ""):
                    with patch.object(mc, "GATEWAY_OAUTH2_SECRET_ARN", ""):
                        result = OAuth2CredentialProvider.from_env()
        assert result is None

    def test_fetches_secret_from_asm(self):
        """Client secret fetched from Secrets Manager when ARN is set."""
        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", "my-client"):
            with patch.object(mc, "GATEWAY_OAUTH2_TOKEN_URL", "https://auth.example.com/token"):
                with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_SECRET", ""):
                    with patch.object(mc, "GATEWAY_OAUTH2_SECRET_ARN", "arn:aws:sm:us-east-1:123:secret:gw-secret"):
                        with patch("workers.mcp_client._fetch_secret_from_asm", return_value="asm-secret"):
                            with patch.object(mc, "GATEWAY_OAUTH2_SCOPE", ""):
                                provider = OAuth2CredentialProvider.from_env()

        assert provider is not None
        assert provider._client_secret == "asm-secret"


class TestGatewayAuthHeaders:
    """Tests for McpGateway._get_auth_headers priority chain."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_oauth2_provider_takes_priority(self):
        """OAuth2 provider headers take priority over static token."""
        mock_provider = MagicMock(spec=OAuth2CredentialProvider)
        mock_provider.get_auth_headers.return_value = {"Authorization": "Bearer oauth2-token"}

        gw = McpGateway(oauth2_provider=mock_provider)
        headers = gw._get_auth_headers()

        assert headers == {"Authorization": "Bearer oauth2-token"}
        mock_provider.get_auth_headers.assert_called_once()

    def test_static_token_when_no_oauth2(self):
        """Static Bearer token used when OAuth2 not configured."""
        gw = McpGateway()
        # Prevent lazy-init of OAuth2 from env
        gw._oauth2_provider = None

        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", ""):
            with patch.object(mc, "GATEWAY_ACCESS_TOKEN", "static-token-xyz"):
                headers = gw._get_auth_headers()

        assert headers == {"Authorization": "Bearer static-token-xyz"}

    def test_no_auth_when_nothing_configured(self):
        """Empty headers when no auth method is configured."""
        gw = McpGateway()
        gw._oauth2_provider = None

        with patch.object(mc, "GATEWAY_OAUTH2_CLIENT_ID", ""):
            with patch.object(mc, "GATEWAY_ACCESS_TOKEN", ""):
                headers = gw._get_auth_headers()

        assert headers == {}

    def test_fallback_to_static_when_oauth2_fails(self):
        """Falls through to static token when OAuth2 returns empty headers."""
        mock_provider = MagicMock(spec=OAuth2CredentialProvider)
        mock_provider.get_auth_headers.return_value = {}  # Token acquisition failed

        gw = McpGateway(oauth2_provider=mock_provider)

        with patch.object(mc, "GATEWAY_ACCESS_TOKEN", "fallback-token"):
            headers = gw._get_auth_headers()

        assert headers == {"Authorization": "Bearer fallback-token"}

    def test_dispose_invalidates_oauth2_provider(self):
        """dispose() calls invalidate on the OAuth2 provider."""
        mock_provider = MagicMock(spec=OAuth2CredentialProvider)
        gw = McpGateway(oauth2_provider=mock_provider)
        gw.dispose()
        mock_provider.invalidate.assert_called_once()


# =========================================================================
# Token-bucket rate limiter tests
# =========================================================================


class TestTokenBucket:
    """Tests for the _TokenBucket rate limiter."""

    def test_unlimited_bucket_always_acquires(self):
        """A bucket with 0 RPM (unlimited) always allows."""
        bucket = _TokenBucket(0)
        for _ in range(1000):
            assert bucket.acquire(timeout=0) is True

    def test_bucket_allows_up_to_capacity(self):
        """A bucket allows up to capacity requests immediately."""
        bucket = _TokenBucket(10)  # 10 RPM
        results = [bucket.acquire(timeout=0) for _ in range(10)]
        assert all(results)

    def test_bucket_blocks_over_capacity(self):
        """A bucket rejects requests beyond capacity when timeout=0."""
        bucket = _TokenBucket(5)  # 5 RPM
        for _ in range(5):
            bucket.acquire(timeout=0)
        assert bucket.acquire(timeout=0) is False

    def test_bucket_refills_over_time(self):
        """Tokens refill based on elapsed time."""
        import time as _time

        bucket = _TokenBucket(60)  # 60 RPM = 1/sec
        for _ in range(60):
            bucket.acquire(timeout=0)
        assert bucket.acquire(timeout=0) is False
        _time.sleep(0.05)  # Wait for ~3 tokens to refill (60/60 * 0.05 = 0.05)
        # After ~50ms at 1 token/sec, should have ~0.05 tokens, not enough
        # Wait a bit more
        _time.sleep(1.0)  # 1 full second = 1 token refilled
        assert bucket.acquire(timeout=0) is True


class TestRateLimiterRegistry:
    """Tests for RateLimiterRegistry."""

    def test_acquire_unknown_server_unlimited(self):
        """Unknown servers default to unlimited."""
        registry = RateLimiterRegistry()
        assert registry.acquire("unknown_server", timeout=0) is True

    def test_acquire_respects_configured_limit(self):
        """Configured servers enforce their rate limit."""
        registry = RateLimiterRegistry({"test_server": 3})
        for _ in range(3):
            assert registry.acquire("test_server", timeout=0) is True
        assert registry.acquire("test_server", timeout=0) is False

    def test_different_servers_independent(self):
        """Each server has its own bucket."""
        registry = RateLimiterRegistry({"a": 2, "b": 2})
        registry.acquire("a", timeout=0)
        registry.acquire("a", timeout=0)
        assert registry.acquire("a", timeout=0) is False
        # Server B should still have tokens
        assert registry.acquire("b", timeout=0) is True


class TestGatewayRateLimiting:
    """Tests for rate limiting in McpGateway.invoke()."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_invoke_rate_limited_returns_error(self):
        """When rate limiter blocks, invoke returns error dict."""
        mock_limiter = MagicMock(spec=RateLimiterRegistry)
        mock_limiter.acquire.return_value = False

        gw = McpGateway(rate_limiter=mock_limiter)
        result = gw.invoke("splunk.search_oneshot", "search_logs", {})

        assert result["error"] == "rate_limited"
        assert result["server"] == "splunk"

    def test_invoke_proceeds_when_rate_limiter_allows(self):
        """When rate limiter allows, invoke proceeds to stub."""
        mock_limiter = MagicMock(spec=RateLimiterRegistry)
        mock_limiter.acquire.return_value = True

        gw = McpGateway(rate_limiter=mock_limiter)
        # No gateway URL set, so falls through to stub
        result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "test"})

        # Should get a stub response, not a rate limit error
        assert "error" not in result or result.get("error") != "rate_limited"
        mock_limiter.acquire.assert_called_once_with("splunk")

    def test_invoke_skips_rate_limit_for_unknown_tool(self):
        """Unknown tools (no server mapping) skip rate limiting."""
        mock_limiter = MagicMock(spec=RateLimiterRegistry)
        gw = McpGateway(rate_limiter=mock_limiter)
        result = gw.invoke("unknown.tool", "action", {})
        # Should not have called acquire since server is empty string
        mock_limiter.acquire.assert_not_called()


# =========================================================================
# OAuth2 401 retry tests
# =========================================================================


class TestGateway401Retry:
    """Tests for 401 retry logic in _invoke_via_gateway."""

    def setup_method(self):
        McpGateway.reset_instance()

    def teardown_method(self):
        McpGateway.reset_instance()

    def test_401_invalidates_token_and_retries(self):
        """401 error invalidates OAuth2 token and retries once."""
        mock_provider = MagicMock(spec=OAuth2CredentialProvider)
        mock_provider.get_auth_headers.return_value = {"Authorization": "Bearer tok"}

        gw = McpGateway(oauth2_provider=mock_provider)

        # Simulate: first call raises 401, retry succeeds (returns stub)
        call_count = 0
        original_get_client = gw._get_mcp_client

        def mock_get_client():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("HTTP 401 Unauthorized")
            return None  # Will fall through to stub

        gw._get_mcp_client = mock_get_client

        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example.com"):
            with patch.object(mc, "_MCP_SDK_AVAILABLE", True):
                result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})

        # Token should have been invalidated
        mock_provider.invalidate.assert_called_once()
        # Result should be a stub (not error), since retry returned None client
        assert "gateway_exception" not in result.get("error", "")

    def test_401_does_not_retry_twice(self):
        """401 on retry does not cause infinite loop."""
        mock_provider = MagicMock(spec=OAuth2CredentialProvider)
        mock_provider.get_auth_headers.return_value = {"Authorization": "Bearer tok"}

        gw = McpGateway(oauth2_provider=mock_provider)

        def always_401():
            raise Exception("HTTP 401 Unauthorized")

        gw._get_mcp_client = always_401

        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example.com"):
            with patch.object(mc, "_MCP_SDK_AVAILABLE", True):
                result = gw.invoke("splunk.search_oneshot", "search_logs", {})

        # Should have errored out after one retry
        assert "error" in result
        assert "401" in result["error"]

    def test_non_401_error_does_not_retry(self):
        """Non-401 errors are not retried."""
        mock_provider = MagicMock(spec=OAuth2CredentialProvider)
        gw = McpGateway(oauth2_provider=mock_provider)

        def raise_500():
            raise Exception("HTTP 500 Internal Server Error")

        gw._get_mcp_client = raise_500

        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example.com"):
            with patch.object(mc, "_MCP_SDK_AVAILABLE", True):
                result = gw.invoke("splunk.search_oneshot", "search_logs", {})

        assert "error" in result
        mock_provider.invalidate.assert_not_called()
