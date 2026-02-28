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
    - GATEWAY_ACCESS_TOKEN env var for CUSTOM_JWT auth, OR
      AWS credentials for AWS_IAM (SigV4) auth

Enterprise architecture (AgentCore gateway pattern):
    Worker -> McpGateway.invoke()
        -> MCPClient.call_tool_sync()
            -> streamablehttp_client (HTTPS + MCP protocol)
                -> AgentCore Gateway (bedrock-agentcore)
                    -> Gateway Target (Lambda / OpenAPI / MCP server)
                        -> Backend API

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
import time
import uuid
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

# Authentication
GATEWAY_ACCESS_TOKEN = os.environ.get("GATEWAY_ACCESS_TOKEN", "")

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
      - Tool name mapping (internal dotted -> gateway triple-underscore)
      - Transport (streamablehttp_client via MCP protocol)
      - Authentication (Bearer token / CUSTOM_JWT)
      - Stub fallback (local dev / tests when gateway URL not set)
      - Structured logging at the transport boundary

    Usage:
        gateway = McpGateway.get_instance()
        result = gateway.invoke("splunk.search_oneshot", "search_logs", params)

    Injection:
        Workers accept an optional gateway parameter in __init__().
        The supervisor injects the shared gateway instance.
    """

    _instance: McpGateway | None = None

    def __init__(self) -> None:
        self._mcp_client = None
        self._tools_cache: dict[str, Any] | None = None
        # Legacy boto3 client for backward compat during migration
        self._boto3_client = None

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
    ) -> dict[str, Any]:
        """Invoke an MCP tool via the AgentCore gateway.

        Routes through the MCP protocol (streamable HTTP) when the gateway
        is configured.  Falls back to legacy invoke_inline_agent if only
        per-server ARNs are set.  Returns stub responses for local dev/tests.

        Args:
            mcp_tool_name: Internal dotted tool name (e.g. "splunk.search_oneshot")
            tool_action: The action/method on the MCP server
            params: Parameters to pass to the tool

        Returns:
            Response dict from the MCP tool, or error dict on failure.
        """
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
    ) -> dict[str, Any]:
        """Invoke via AgentCore gateway using MCPClient + streamablehttp_client."""
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
            logger.error(
                "MCP gateway call failed: tool=%s error=%s elapsed=%.1fms",
                mcp_tool_name, exc, elapsed_ms,
            )
            return {"error": f"gateway_exception: {exc}", "tool": mcp_tool_name}

    def _get_mcp_client(self):
        """Lazily create the MCPClient connected to the AgentCore gateway."""
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

            headers = {}
            if GATEWAY_ACCESS_TOKEN:
                headers["Authorization"] = f"Bearer {GATEWAY_ACCESS_TOKEN}"

            self._mcp_client = MCPClient(
                lambda: streamablehttp_client(
                    url=gateway_url,
                    headers=headers,
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
        """Release clients and cached state."""
        self._mcp_client = None
        self._boto3_client = None
        self._tools_cache = None


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
