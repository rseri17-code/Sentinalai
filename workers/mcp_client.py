"""AgentCore MCP Gateway Client.

Provides a unified gateway that fronts all MCP tool targets deployed on
Amazon Bedrock AgentCore.  Every backend API (Moogsoft, Splunk, Sysdig,
SignalFx, Dynatrace, ServiceNow, GitHub) is registered as a gateway target
and accessed through a single AgentCore gateway URL via the MCP protocol.

Workers MUST call MCP tools through the gateway — never directly via boto3
or HTTP.  The gateway handles authentication, tool routing, transport,
response parsing, structured logging, and stub fallback for local dev/tests.

Requires (production):
    - strands-agents SDK  (strands.tools.mcp.MCPClient)
    - mcp SDK             (mcp.client.streamable_http)
    - AGENTCORE_GATEWAY_URL env var set to the gateway endpoint
    - OAuth2 client credentials (GATEWAY_OAUTH2_CLIENT_ID + token URL), OR
    - GATEWAY_ACCESS_TOKEN env var for static CUSTOM_JWT auth, OR
      AWS credentials for AWS_IAM (SigV4) auth

Authentication priority:
    1. OAuth2 client_credentials grant (if GATEWAY_OAUTH2_CLIENT_ID is set)
       - Automatic token acquisition from Cognito token endpoint
       - In-memory caching with 10-minute pre-expiry refresh
       - Client secret from env var or AWS Secrets Manager
    2. Static Bearer token (if GATEWAY_ACCESS_TOKEN is set)
    3. No auth (local dev / tests)

Enterprise architecture (AgentCore gateway pattern):
    Worker -> McpGateway.invoke()
        -> MCPClient.call_tool_sync()
            -> streamablehttp_client (HTTPS + MCP protocol)
                -> AgentCore Gateway (bedrock-agentcore)
                    -> Gateway Target (Lambda / OpenAPI / MCP server)
                        -> Backend API

OAuth2 two-legged flow (client_credentials):
    Agent (TOKEN-A) -> AgentCore Gateway
        Gateway validates TOKEN-A, maps audience
        Gateway mints TOKEN-B (per-resource) via credential provider
        Gateway -> Resource MCP Server (TOKEN-B) -> Backend API

Gateway target naming convention:
    Tools exposed through the gateway use triple-underscore naming:
        {TARGET_NAME}___{OPERATION_NAME}
    Example: "SplunkTarget___search_oneshot"

    Workers use dotted names internally (e.g. "splunk.search_oneshot"),
    which the gateway maps to the actual gateway tool name at invocation.

Servers fronted by this gateway:
    - moogsoft     (AIOPS / incident management)
    - splunk       (log analytics / change data)
    - sysdig       (infrastructure metrics / events)
    - signalfx     (APM metrics)
    - dynatrace    (APM / problems / entities)
    - servicenow   (ITSM / CMDB / change management)
    - github       (DevOps / CI-CD / code changes)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("sentinalai.mcp_client")

# ---------------------------------------------------------------------------
# Optional SDK imports (graceful — tests run without them)
# ---------------------------------------------------------------------------

try:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_SDK_AVAILABLE = True
except ImportError:
    _MCP_SDK_AVAILABLE = False
    MCPClient = None  # type: ignore[assignment,misc]

# Legacy boto3 import (kept for backward-compat in _parse_agent_response)
try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError as _ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False
    _ClientError = None


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# AgentCore Gateway URL — single endpoint for all MCP targets
AGENTCORE_GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")

# Authentication — static Bearer token (fallback if OAuth2 not configured)
GATEWAY_ACCESS_TOKEN = os.environ.get("GATEWAY_ACCESS_TOKEN", "")

# OAuth2 client_credentials configuration (preferred auth method)
GATEWAY_OAUTH2_CLIENT_ID = os.environ.get("GATEWAY_OAUTH2_CLIENT_ID", "")
GATEWAY_OAUTH2_CLIENT_SECRET = os.environ.get("GATEWAY_OAUTH2_CLIENT_SECRET", "")
GATEWAY_OAUTH2_TOKEN_URL = os.environ.get("GATEWAY_OAUTH2_TOKEN_URL", "")
GATEWAY_OAUTH2_SCOPE = os.environ.get("GATEWAY_OAUTH2_SCOPE", "")
# Optional: ARN of Secrets Manager secret holding client_secret
GATEWAY_OAUTH2_SECRET_ARN = os.environ.get("GATEWAY_OAUTH2_SECRET_ARN", "")
# Optional: Cognito User Pool ID (to auto-derive token_url if not provided)
GATEWAY_COGNITO_USER_POOL_ID = os.environ.get("GATEWAY_COGNITO_USER_POOL_ID", "")
GATEWAY_COGNITO_DOMAIN = os.environ.get("GATEWAY_COGNITO_DOMAIN", "")
# Token refresh buffer (seconds before expiry to trigger refresh)
_TOKEN_REFRESH_BUFFER = int(os.environ.get("GATEWAY_TOKEN_REFRESH_BUFFER_SECONDS", "600"))

# Legacy per-server ARNs (backward compat — deprecated in favor of gateway URL)
MCP_TOOL_ARNS: dict[str, str] = {
    "moogsoft": os.environ.get("MCP_MOOGSOFT_TOOL_ARN", ""),
    "splunk": os.environ.get("MCP_SPLUNK_TOOL_ARN", ""),
    "sysdig": os.environ.get("MCP_SYSDIG_TOOL_ARN", ""),
    "signalfx": os.environ.get("MCP_SIGNALFX_TOOL_ARN", ""),
    "dynatrace": os.environ.get("MCP_DYNATRACE_TOOL_ARN", ""),
    "servicenow": os.environ.get("MCP_SERVICENOW_TOOL_ARN", ""),
    "github": os.environ.get("MCP_GITHUB_TOOL_ARN", ""),
}

# Retry / timeout config
MCP_CALL_TIMEOUT = int(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "30"))
MCP_MAX_RETRIES = int(os.environ.get("MCP_MAX_RETRIES", "2"))


def _has_any_arn() -> bool:
    """Check if any MCP tool ARN is configured (legacy check)."""
    return any(arn for arn in MCP_TOOL_ARNS.values())


# Optional HTTP library for OAuth2 token requests
try:
    import requests as _requests_lib
    _REQUESTS_AVAILABLE = True
except ImportError:
    _requests_lib = None  # type: ignore[assignment]
    _REQUESTS_AVAILABLE = False


# =========================================================================
# OAuth2 Credential Provider — client_credentials grant with token caching
# =========================================================================

class OAuth2CredentialProvider:
    """Manages OAuth2 access tokens via client_credentials grant.

    Implements the two-legged OAuth flow used by AgentCore gateways:
    - Agent authenticates to Cognito with client_id + client_secret
    - Receives an M2M access token (TOKEN-A) with configured scopes
    - Token is cached in memory and refreshed automatically before expiry

    The AgentCore gateway then validates TOKEN-A and uses its own
    credential provider to mint per-resource tokens (TOKEN-B/C) for
    downstream MCP targets (Splunk, ServiceNow, GitHub, etc.).

    Thread-safe: uses a lock for token refresh operations.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str,
        scope: str = "",
        refresh_buffer_seconds: int = 600,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scope = scope
        self._refresh_buffer = timedelta(seconds=refresh_buffer_seconds)
        self._token: str | None = None
        self._expiry: datetime | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> OAuth2CredentialProvider | None:
        """Create a provider from environment variables, or None if not configured.

        Resolves the token URL from either:
        - GATEWAY_OAUTH2_TOKEN_URL (explicit)
        - GATEWAY_COGNITO_DOMAIN + AWS_REGION (Cognito convention)

        Resolves client_secret from either:
        - GATEWAY_OAUTH2_CLIENT_SECRET (env var)
        - GATEWAY_OAUTH2_SECRET_ARN (fetched from Secrets Manager at init)
        """
        client_id = GATEWAY_OAUTH2_CLIENT_ID
        if not client_id:
            return None

        # Resolve token URL
        token_url = GATEWAY_OAUTH2_TOKEN_URL
        if not token_url and GATEWAY_COGNITO_DOMAIN:
            token_url = (
                f"https://{GATEWAY_COGNITO_DOMAIN}"
                f".auth.{AWS_REGION}.amazoncognito.com/oauth2/token"
            )
        if not token_url:
            logger.warning(
                "GATEWAY_OAUTH2_CLIENT_ID is set but no token URL configured "
                "(set GATEWAY_OAUTH2_TOKEN_URL or GATEWAY_COGNITO_DOMAIN)"
            )
            return None

        # Resolve client secret
        client_secret = GATEWAY_OAUTH2_CLIENT_SECRET
        if not client_secret and GATEWAY_OAUTH2_SECRET_ARN:
            client_secret = _fetch_secret_from_asm(GATEWAY_OAUTH2_SECRET_ARN)
        if not client_secret:
            logger.warning(
                "GATEWAY_OAUTH2_CLIENT_ID is set but no client secret configured "
                "(set GATEWAY_OAUTH2_CLIENT_SECRET or GATEWAY_OAUTH2_SECRET_ARN)"
            )
            return None

        scope = GATEWAY_OAUTH2_SCOPE
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            scope=scope,
            refresh_buffer_seconds=_TOKEN_REFRESH_BUFFER,
        )

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if expired or near-expiry.

        Uses double-checked locking to minimize contention: concurrent
        callers skip the lock entirely when a valid cached token exists.
        Only the first thread to detect expiry acquires the lock and
        performs the HTTP refresh; late arrivals recheck after acquiring
        the lock and reuse the freshly-refreshed token.

        Returns empty string if token acquisition fails.
        """
        # Fast path (no lock): return cached token if still valid
        now = datetime.now(timezone.utc)
        if self._token and self._expiry and now < self._expiry:
            return self._token

        # Slow path: acquire lock, recheck, refresh if still expired
        with self._lock:
            now = datetime.now(timezone.utc)
            if self._token and self._expiry and now < self._expiry:
                return self._token
            return self._refresh()

    def get_auth_headers(self) -> dict[str, str]:
        """Return Authorization headers with a valid Bearer token."""
        token = self.get_access_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def invalidate(self) -> None:
        """Force token refresh on next call (e.g., after a 401 response)."""
        with self._lock:
            self._token = None
            self._expiry = None

    def _refresh(self) -> str:
        """Acquire a new token via client_credentials grant.

        Called under lock. Returns the new token or empty string on failure.
        """
        if not _REQUESTS_AVAILABLE:
            logger.warning("requests library not installed — OAuth2 token refresh disabled")
            return ""

        try:
            data: dict[str, str] = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scope:
                data["scope"] = self._scope

            response = _requests_lib.post(
                self._token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=data,
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()

            self._token = body["access_token"]
            expires_in = body.get("expires_in", 3600)
            self._expiry = (
                datetime.now(timezone.utc)
                + timedelta(seconds=expires_in)
                - self._refresh_buffer
            )
            logger.info(
                "OAuth2 token acquired: expires_in=%ds scope=%s",
                expires_in, self._scope or "(default)",
            )
            return self._token

        except Exception as exc:
            logger.error("OAuth2 token refresh failed: %s", exc)
            self._token = None
            self._expiry = None
            return ""


def _fetch_secret_from_asm(secret_arn: str) -> str:
    """Fetch a client_secret from AWS Secrets Manager.

    Returns the secret string, or empty string on failure.
    Used when GATEWAY_OAUTH2_SECRET_ARN is set instead of a direct secret.
    """
    if not _BOTO3_AVAILABLE:
        logger.warning("boto3 not available — cannot fetch secret from Secrets Manager")
        return ""
    try:
        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        response = sm.get_secret_value(SecretId=secret_arn)
        secret_str = response.get("SecretString", "")
        # Support both plain string and JSON {"client_secret": "..."} formats
        if secret_str.startswith("{"):
            try:
                secret_dict = json.loads(secret_str)
                return secret_dict.get("client_secret", secret_dict.get("secret", secret_str))
            except (json.JSONDecodeError, TypeError):
                pass
        return secret_str
    except Exception as exc:
        logger.error("Failed to fetch secret from Secrets Manager (%s): %s", secret_arn, exc)
        return ""


# ---------------------------------------------------------------------------
# MCP Tool name -> server mapping (all 7 servers)
# ---------------------------------------------------------------------------

_TOOL_TO_SERVER: dict[str, str] = {
    # Moogsoft (AIOPS)
    "moogsoft.get_incident_by_id": "moogsoft",
    "moogsoft.get_incidents": "moogsoft",
    "moogsoft.get_critical_incidents": "moogsoft",
    "moogsoft.get_alerts": "moogsoft",
    "moogsoft.get_historical_analysis": "moogsoft",
    "moogsoft.get_closed_incidents": "moogsoft",
    # Splunk (logs / change data)
    "splunk.search_oneshot": "splunk",
    "splunk.search_export": "splunk",
    "splunk.get_change_data": "splunk",
    "splunk.app_change_data": "splunk",
    "splunk.get_host_metrics": "splunk",
    "splunk.get_health_status": "splunk",
    "splunk.get_incident_data": "splunk",
    # Sysdig (infrastructure metrics / events)
    "sysdig.query_metrics": "sysdig",
    "sysdig.golden_signals": "sysdig",
    "sysdig.get_events": "sysdig",
    "sysdig.discover_resources": "sysdig",
    "sysdig.environment_status": "sysdig",
    # SignalFx (APM)
    "signalfx.query_signalfx_metrics": "signalfx",
    "signalfx.get_signalfx_active_incidents": "signalfx",
    # Dynatrace (APM / problems)
    "dynatrace.get_problems": "dynatrace",
    "dynatrace.get_metrics": "dynatrace",
    "dynatrace.get_entities": "dynatrace",
    "dynatrace.get_events": "dynatrace",
    # ServiceNow (ITSM / CMDB)
    "servicenow.get_ci_details": "servicenow",
    "servicenow.search_incidents": "servicenow",
    "servicenow.get_change_records": "servicenow",
    "servicenow.get_known_errors": "servicenow",
    # GitHub (DevOps / CI-CD)
    "github.get_recent_deployments": "github",
    "github.get_pr_details": "github",
    "github.get_commit_diff": "github",
    "github.get_workflow_runs": "github",
}

# Server -> default AgentCore gateway target name mapping.
# These are the target names configured when creating the gateway via
# bedrock-agentcore-control.create_gateway_target().
# Override via AGENTCORE_TARGET_{SERVER} env vars for custom target names.
_SERVER_TO_TARGET: dict[str, str] = {
    "moogsoft": os.environ.get("AGENTCORE_TARGET_MOOGSOFT", "MoogsoftTarget"),
    "splunk": os.environ.get("AGENTCORE_TARGET_SPLUNK", "SplunkTarget"),
    "sysdig": os.environ.get("AGENTCORE_TARGET_SYSDIG", "SysdigTarget"),
    "signalfx": os.environ.get("AGENTCORE_TARGET_SIGNALFX", "SignalFxTarget"),
    "dynatrace": os.environ.get("AGENTCORE_TARGET_DYNATRACE", "DynatraceTarget"),
    "servicenow": os.environ.get("AGENTCORE_TARGET_SERVICENOW", "ServiceNowTarget"),
    "github": os.environ.get("AGENTCORE_TARGET_GITHUB", "GitHubTarget"),
}


def _to_gateway_tool_name(mcp_tool_name: str) -> str:
    """Convert internal dotted name to AgentCore gateway tool name.

    Internal:  "splunk.search_oneshot"
    Gateway:   "SplunkTarget___search_oneshot"

    The gateway uses triple-underscore to separate target name from operation.
    """
    server = _TOOL_TO_SERVER.get(mcp_tool_name, "")
    if not server:
        return mcp_tool_name
    target = _SERVER_TO_TARGET.get(server, "")
    if not target:
        return mcp_tool_name
    # Extract operation from dotted name: "splunk.search_oneshot" -> "search_oneshot"
    parts = mcp_tool_name.split(".", 1)
    operation = parts[1] if len(parts) > 1 else mcp_tool_name
    return f"{target}___{operation}"


# =========================================================================
# Token-bucket rate limiter (per MCP server)
# =========================================================================

# Default rate limits per MCP server (requests-per-minute, 0 = unlimited)
_DEFAULT_RATE_LIMITS: dict[str, int] = {
    "moogsoft": 60,
    "splunk": 0,      # unlimited
    "sysdig": 100,
    "signalfx": 60,
    "dynatrace": 100,
    "servicenow": 60,
    "github": 30,
}


class _TokenBucket:
    """Thread-safe token-bucket rate limiter for a single server.

    Efficiency: non-blocking fast path when tokens are available.
    Sleep is capped at 0.5s per iteration to prevent thread starvation.
    """

    __slots__ = ("_capacity", "_tokens", "_refill_rate", "_last_refill", "_lock")

    # Max sleep per iteration to avoid thread starvation in concurrent workloads
    _MAX_SLEEP_SECONDS = 0.5

    def __init__(self, requests_per_minute: int) -> None:
        # 0 means unlimited — set very large capacity
        if requests_per_minute <= 0:
            self._capacity = float("inf")
            self._tokens = float("inf")
            self._refill_rate = 0.0
        else:
            self._capacity = float(requests_per_minute)
            self._tokens = float(requests_per_minute)
            self._refill_rate = requests_per_minute / 60.0  # tokens per second
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 5.0) -> bool:
        """Try to acquire a token.  Returns True if allowed, False if rate-limited.

        Refills tokens based on elapsed time, then consumes one.
        Blocks up to *timeout* seconds waiting for a token to become available.
        Sleep is capped per iteration to prevent thread starvation under
        concurrent ThreadPoolExecutor workloads.
        """
        if self._capacity == float("inf"):
            return True

        deadline = time.monotonic() + timeout
        with self._lock:
            first_try = True
            while True:
                now = time.monotonic()
                # Allow the first attempt even with timeout=0
                if not first_try and now >= deadline:
                    return False
                first_try = False

                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                # Non-blocking: if timeout=0, fail immediately after first check
                if timeout <= 0:
                    return False

                # How long until one token is available?
                wait = (1.0 - self._tokens) / self._refill_rate if self._refill_rate > 0 else timeout + 1
                if now + wait > deadline:
                    return False

                # Release lock while waiting, then reacquire
                # Cap sleep to prevent thread starvation in pooled executors
                sleep_time = min(wait, deadline - now, self._MAX_SLEEP_SECONDS)
                self._lock.release()
                try:
                    time.sleep(sleep_time)
                finally:
                    self._lock.acquire()


class RateLimiterRegistry:
    """Registry of per-server token-bucket rate limiters.

    Set ``unlimited=True`` (or env RATE_LIMITER_DISABLED=1) to bypass all
    rate limiting — useful for tests and local dev where stub responses
    are instant and rate limits cause unnecessary thread contention.
    """

    def __init__(
        self,
        limits: dict[str, int] | None = None,
        unlimited: bool = False,
    ) -> None:
        self._limits = limits or _DEFAULT_RATE_LIMITS
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()
        self._unlimited = unlimited or os.environ.get("RATE_LIMITER_DISABLED", "").lower() in ("1", "true", "yes")

    def acquire(self, server: str, timeout: float = 5.0) -> bool:
        """Acquire a rate-limit token for *server*.  Returns False if blocked."""
        if self._unlimited:
            return True
        bucket = self._get_bucket(server)
        return bucket.acquire(timeout)

    def _get_bucket(self, server: str) -> _TokenBucket:
        with self._lock:
            if server not in self._buckets:
                rpm = self._limits.get(server, 0)  # default: unlimited
                self._buckets[server] = _TokenBucket(rpm)
            return self._buckets[server]


# =========================================================================
# McpGateway — singleton class fronting all MCP servers via AgentCore
# =========================================================================

class McpGateway:
    """Unified gateway that fronts all MCP tool targets on AgentCore.

    Uses the MCP protocol over streamable HTTP to communicate with the
    AgentCore gateway.  This is the production-grade pattern from the
    amazon-bedrock-agentcore-samples SRE agent reference implementation.

    Every worker MUST route MCP calls through a gateway instance.
    The gateway owns:
      - MCPClient lifecycle (lazy init, singleton)
      - OAuth2 credential provider (client_credentials + token caching)
      - Tool name mapping (internal dotted -> gateway triple-underscore)
      - Transport (streamablehttp_client via MCP protocol)
      - Stub fallback (local dev / tests when gateway URL not set)
      - Structured logging at the transport boundary

    Authentication priority:
      1. OAuth2 client_credentials (if GATEWAY_OAUTH2_CLIENT_ID is set)
      2. Static Bearer token (if GATEWAY_ACCESS_TOKEN is set)
      3. No auth headers (local dev / tests)

    Usage:
        gateway = McpGateway.get_instance()
        result = gateway.invoke("splunk.search_oneshot", "search_logs", params)

    Injection:
        Workers accept an optional gateway parameter in __init__().
        The supervisor injects the shared gateway instance.
    """

    _instance: McpGateway | None = None

    def __init__(
        self,
        oauth2_provider: OAuth2CredentialProvider | None = None,
        rate_limiter: RateLimiterRegistry | None = None,
    ) -> None:
        self._mcp_client = None
        self._tools_cache: dict[str, Any] | None = None
        # Legacy boto3 client for backward compat during migration
        self._boto3_client = None
        # OAuth2 provider (lazy-init from env if not injected)
        self._oauth2_provider = oauth2_provider
        # Fast-path: when no gateway is configured, skip rate limiting for stubs
        # (stubs are instant in-memory responses — no external service to protect)
        stub_mode = not AGENTCORE_GATEWAY_URL
        self._rate_limiter = rate_limiter or RateLimiterRegistry(
            unlimited=stub_mode,
        )

    @classmethod
    def get_instance(cls) -> McpGateway:
        """Return the singleton gateway.  Thread-safe for read-only access."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for tests only)."""
        if cls._instance is not None:
            cls._instance.dispose()
        cls._instance = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def invoke(
        self,
        mcp_tool_name: str,
        tool_action: str,
        params: dict[str, Any],
        user_identity: str | None = None,
    ) -> dict[str, Any]:
        """Invoke an MCP tool via the AgentCore gateway.

        Routes through the MCP protocol (streamable HTTP) when the gateway
        is configured.  Falls back to legacy invoke_inline_agent if only
        per-server ARNs are set.  Returns stub responses for local dev/tests.

        Args:
            mcp_tool_name: Internal dotted tool name (e.g. "splunk.search_oneshot")
            tool_action: The action/method on the MCP server
            params: Parameters to pass to the tool
            user_identity: Optional user identity string to propagate via
                X-User-Identity header for downstream authorization (G3.2).

        Returns:
            Response dict from the MCP tool, or error dict on failure.
        """
        # G3.2: Store user identity for header propagation
        self._current_user_identity = user_identity

        # Rate-limit check (per-server token bucket)
        server = _TOOL_TO_SERVER.get(mcp_tool_name, "")
        if server and not self._rate_limiter.acquire(server):
            logger.warning(
                "Rate limited: server=%s tool=%s", server, mcp_tool_name,
            )
            return {"error": "rate_limited", "server": server, "tool": mcp_tool_name}

        # Priority 1: AgentCore gateway (MCP protocol — production path)
        if AGENTCORE_GATEWAY_URL and _MCP_SDK_AVAILABLE:
            return self._invoke_via_gateway(mcp_tool_name, tool_action, params)

        # Priority 2: Legacy per-server ARNs (invoke_inline_agent)
        arn = self.get_arn_for_tool(mcp_tool_name)
        if arn:
            return self._invoke_via_legacy(mcp_tool_name, tool_action, params, arn)

        # Priority 3: Stub responses (local dev / tests)
        logger.debug("No gateway or ARN configured for %s — returning stub", mcp_tool_name)
        return _stub_response(mcp_tool_name, tool_action, params)

    # ------------------------------------------------------------------ #
    # AgentCore gateway invocation (production path)
    # ------------------------------------------------------------------ #

    def _invoke_via_gateway(
        self, mcp_tool_name: str, tool_action: str, params: dict[str, Any],
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Invoke via AgentCore gateway using MCPClient + streamablehttp_client.

        On 401 (Unauthorized), invalidates the OAuth2 token and retries once
        to handle token expiry during mid-flight requests.
        """
        gateway_tool_name = _to_gateway_tool_name(mcp_tool_name)
        tool_use_id = f"sentinalai-{uuid.uuid4().hex[:12]}"

        start = time.monotonic()
        try:
            client = self._get_mcp_client()
            if client is None:
                logger.warning("MCPClient unavailable — returning stub for %s", mcp_tool_name)
                return _stub_response(mcp_tool_name, tool_action, params)

            result = client.call_tool_sync(
                tool_use_id=tool_use_id,
                name=gateway_tool_name,
                arguments=params,
            )

            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info(
                "MCP gateway call: tool=%s gateway_name=%s elapsed=%.1fms",
                mcp_tool_name, gateway_tool_name, elapsed_ms,
            )

            # MCPClient returns tool result content — normalize to dict
            if isinstance(result, dict):
                return result
            if hasattr(result, "content"):
                # MCP ToolResult has a .content field (list of TextContent)
                content_parts = result.content if hasattr(result, "content") else []
                text_parts = []
                for part in content_parts:
                    if hasattr(part, "text"):
                        text_parts.append(part.text)
                combined = "\n".join(text_parts)
                try:
                    return json.loads(combined)
                except (json.JSONDecodeError, TypeError):
                    return {"raw_response": combined}
            return {"raw_response": str(result)}

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000

            # 401 retry: invalidate OAuth2 token and retry once
            is_401 = "401" in str(exc) or "Unauthorized" in str(exc)
            if is_401 and not _is_retry and self._oauth2_provider is not None:
                logger.warning(
                    "MCP gateway 401 for %s — invalidating token and retrying",
                    mcp_tool_name,
                )
                self._oauth2_provider.invalidate()
                # Force new MCPClient with fresh auth headers
                self._mcp_client = None
                return self._invoke_via_gateway(
                    mcp_tool_name, tool_action, params, _is_retry=True,
                )

            logger.error(
                "MCP gateway call failed: tool=%s error=%s elapsed=%.1fms",
                mcp_tool_name, exc, elapsed_ms,
            )
            return {"error": f"gateway_exception: {exc}", "tool": mcp_tool_name}

    def _get_auth_headers(self) -> dict[str, str]:
        """Resolve authentication headers using the configured auth method.

        Priority:
            1. OAuth2 credential provider (client_credentials with auto-refresh)
            2. Static Bearer token (GATEWAY_ACCESS_TOKEN env var)
            3. Empty dict (no auth — local dev / tests)

        G3.2: Includes X-User-Identity header when user identity is available.
        """
        headers: dict[str, str] = {}

        # Lazy-init OAuth2 provider from env on first call
        if self._oauth2_provider is None:
            self._oauth2_provider = OAuth2CredentialProvider.from_env()

        # Priority 1: OAuth2 client_credentials
        if self._oauth2_provider is not None:
            auth_headers = self._oauth2_provider.get_auth_headers()
            if auth_headers:
                headers.update(auth_headers)
            # OAuth2 configured but token acquisition failed — fall through

        # Priority 2: Static Bearer token
        if not headers and GATEWAY_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {GATEWAY_ACCESS_TOKEN}"

        # G3.2: Propagate user identity to downstream MCPs
        user_identity = getattr(self, "_current_user_identity", None)
        if user_identity:
            headers["X-User-Identity"] = user_identity

        return headers

    def _get_mcp_client(self):
        """Lazily create the MCPClient connected to the AgentCore gateway.

        The transport factory lambda calls _get_auth_headers() on each
        connection so that refreshed OAuth2 tokens are picked up automatically.
        """
        if self._mcp_client is not None:
            return self._mcp_client
        if not _MCP_SDK_AVAILABLE:
            logger.debug("strands/mcp SDK not installed — MCP gateway disabled")
            return None
        if not AGENTCORE_GATEWAY_URL:
            return None
        try:
            gateway_url = AGENTCORE_GATEWAY_URL
            if not gateway_url.endswith("/mcp"):
                gateway_url = f"{gateway_url}/mcp"

            # Capture self for the lambda so auth headers are resolved
            # dynamically on each connection (picks up refreshed tokens).
            gw_self = self

            self._mcp_client = MCPClient(
                lambda: streamablehttp_client(
                    url=gateway_url,
                    headers=gw_self._get_auth_headers(),
                ),
            )
            logger.info("MCPClient connected to AgentCore gateway: %s", gateway_url)
            return self._mcp_client
        except Exception as exc:
            logger.warning("Failed to create MCPClient: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Legacy invocation (invoke_inline_agent — deprecated)
    # ------------------------------------------------------------------ #

    def _invoke_via_legacy(
        self, mcp_tool_name: str, tool_action: str, params: dict[str, Any], arn: str,
    ) -> dict[str, Any]:
        """Legacy: invoke via bedrock-agent-runtime invoke_inline_agent.

        Deprecated in favor of AgentCore gateway.  Kept for backward
        compatibility during migration.
        """
        client = self._get_boto3_client()
        if client is None:
            logger.warning("No bedrock-agent-runtime client — returning stub for %s", mcp_tool_name)
            return _stub_response(mcp_tool_name, tool_action, params)

        start = time.monotonic()
        try:
            response = client.invoke_inline_agent(
                inputText=json.dumps({
                    "tool": mcp_tool_name,
                    "action": tool_action,
                    "parameters": params,
                }),
                sessionId=params.get("session_id", "sentinalai-default"),
                enableTrace=True,
                inlineSessionState={
                    "invocationId": f"mcp-{mcp_tool_name}-{int(time.time())}",
                },
            )

            elapsed_ms = (time.monotonic() - start) * 1000
            result = _parse_agent_response(response)

            logger.info(
                "MCP legacy call: tool=%s action=%s elapsed=%.1fms",
                mcp_tool_name, tool_action, elapsed_ms,
            )
            return result

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            if _BOTO3_AVAILABLE and _ClientError and isinstance(exc, _ClientError):
                error_code = exc.response.get("Error", {}).get("Code", "Unknown")
                logger.error(
                    "MCP call failed: tool=%s error=%s elapsed=%.1fms",
                    mcp_tool_name, error_code, elapsed_ms,
                )
                return {"error": f"mcp_call_failed: {error_code}", "tool": mcp_tool_name}
            logger.error(
                "MCP call exception: tool=%s error=%s elapsed=%.1fms",
                mcp_tool_name, exc, elapsed_ms,
            )
            return {"error": f"mcp_exception: {exc}", "tool": mcp_tool_name}

    def _get_boto3_client(self):
        """Lazily create the bedrock-agent-runtime boto3 client (legacy)."""
        if self._boto3_client is not None:
            return self._boto3_client
        if not _BOTO3_AVAILABLE:
            logger.debug("boto3 not installed — legacy MCP calls disabled")
            return None
        try:
            self._boto3_client = boto3.client(
                "bedrock-agent-runtime",
                region_name=AWS_REGION,
                config=BotoConfig(
                    retries={"max_attempts": MCP_MAX_RETRIES, "mode": "adaptive"},
                    connect_timeout=10,
                    read_timeout=MCP_CALL_TIMEOUT,
                ),
            )
            return self._boto3_client
        except Exception as exc:
            logger.warning("Failed to create bedrock-agent-runtime client: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Resolution helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_server_for_tool(mcp_tool_name: str) -> str:
        """Map an MCP tool name to its server."""
        return _TOOL_TO_SERVER.get(mcp_tool_name, "")

    @staticmethod
    def get_arn_for_tool(mcp_tool_name: str) -> str:
        """Get the AgentCore tool ARN for an MCP tool name (legacy)."""
        server = _TOOL_TO_SERVER.get(mcp_tool_name, "")
        return MCP_TOOL_ARNS.get(server, "")

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #

    def dispose(self) -> None:
        """Release clients, tokens, and cached state."""
        self._mcp_client = None
        self._boto3_client = None
        self._tools_cache = None
        if self._oauth2_provider is not None:
            self._oauth2_provider.invalidate()


# =========================================================================
# Module-level backward-compatible API
# =========================================================================
# These functions delegate to the singleton McpGateway so that existing
# code using `from workers.mcp_client import invoke_mcp_tool` continues
# to work without changes.  New code should use the gateway instance.

_client = None  # legacy — kept for test_mcp_client_coverage compatibility


def _get_client():
    """Legacy: lazily create the bedrock-agent-runtime boto3 client."""
    gw = McpGateway.get_instance()
    return gw._get_boto3_client()


def get_server_for_tool(mcp_tool_name: str) -> str:
    """Map an MCP tool name to its server."""
    return _TOOL_TO_SERVER.get(mcp_tool_name, "")


def get_arn_for_tool(mcp_tool_name: str) -> str:
    """Get the AgentCore tool ARN for an MCP tool name (legacy)."""
    server = get_server_for_tool(mcp_tool_name)
    return MCP_TOOL_ARNS.get(server, "")


def invoke_mcp_tool(
    mcp_tool_name: str,
    tool_action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Invoke an MCP tool via the singleton McpGateway.

    Backward-compatible wrapper.  Workers that accept a gateway
    parameter should call gateway.invoke() directly instead.
    """
    return McpGateway.get_instance().invoke(mcp_tool_name, tool_action, params)


def dispose() -> None:
    """Release all clients (legacy + gateway)."""
    global _client
    _client = None
    McpGateway.get_instance().dispose()


# =========================================================================
# Response parsing (legacy — for invoke_inline_agent responses)
# =========================================================================

def _parse_agent_response(response: dict) -> dict[str, Any]:
    """Parse the bedrock-agent-runtime streaming response into a dict."""
    completion = response.get("completion", [])
    result_text = ""

    if isinstance(completion, str):
        result_text = completion
    elif isinstance(completion, list):
        for event in completion:
            if isinstance(event, dict):
                chunk = event.get("chunk", {})
                if isinstance(chunk, dict) and "bytes" in chunk:
                    result_text += chunk["bytes"].decode("utf-8", errors="replace")
    elif hasattr(completion, "__iter__"):
        for event in completion:
            if isinstance(event, dict):
                chunk = event.get("chunk", {})
                if isinstance(chunk, dict) and "bytes" in chunk:
                    result_text += chunk["bytes"].decode("utf-8", errors="replace")

    # Try to parse as JSON
    if result_text:
        try:
            return json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return {"raw_response": result_text}

    return {"raw_response": result_text or "empty"}


# =========================================================================
# Stub responses (fallback when gateway not configured)
# =========================================================================

def _stub_response(mcp_tool_name: str, tool_action: str, params: dict) -> dict:
    """Return a minimal stub response for local dev / tests."""
    server = get_server_for_tool(mcp_tool_name)

    if server == "moogsoft":
        incident_id = params.get("incident_id", "unknown")
        return {"incident": {"incident_id": incident_id, "status": "pending"}}

    if server == "splunk":
        if "change" in tool_action.lower():
            return {"changes": []}
        return {"logs": {"results": [], "count": 0}}

    if server == "sysdig":
        if "event" in tool_action.lower():
            return {"events": []}
        if "golden" in tool_action.lower() or "signal" in tool_action.lower():
            return {"signals": {}}
        return {"metrics": {"metrics": [], "baseline": 0}}

    if server == "signalfx":
        if "golden" in tool_action.lower() or "signal" in tool_action.lower():
            return {"signals": {}}
        return {"metrics": {}}

    if server == "dynatrace":
        if "problem" in tool_action.lower():
            return {"problems": []}
        if "event" in tool_action.lower():
            return {"events": []}
        return {"metrics": {}}

    if server == "servicenow":
        if "incident" in tool_action.lower():
            return {"incidents": []}
        if "ci_detail" in tool_action.lower() or tool_action.lower() == "get_ci_details":
            return {"ci": {}}
        if "change" in tool_action.lower():
            return {"change_records": []}
        if "known" in tool_action.lower() or "error" in tool_action.lower():
            return {"known_errors": []}
        return {"ci": {}}

    if server == "github":
        if "deployment" in tool_action.lower():
            return {"deployments": []}
        if "pr" in tool_action.lower():
            return {"pr": {}}
        if "commit" in tool_action.lower() or "diff" in tool_action.lower():
            return {"commit": {}}
        if "workflow" in tool_action.lower():
            return {"workflow_runs": []}
        return {}

    return {}
