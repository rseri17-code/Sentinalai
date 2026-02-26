"""AgentCore MCP Gateway Client.

Invokes MCP tools deployed as AgentCore runtime engines via their ARNs.
Each MCP server (Moogsoft, Splunk, Sysdig, SignalFx) is addressed by its
AgentCore tool ARN.  When ARNs are not configured, falls back to returning
stub responses so local dev and tests continue to work.

Requires:
    - boto3 with bedrock-agent-runtime service support
    - MCP_*_TOOL_ARN env vars set for each MCP server

Enterprise architecture:
    Agent (this code) -> AgentCore Gateway -> MCP Runtime Engine -> Backend API
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("sentinalai.mcp_client")

# ---------------------------------------------------------------------------
# Optional boto3 import (graceful — tests run without it)
# ---------------------------------------------------------------------------

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

# MCP Tool ARNs — one per MCP server deployed on AgentCore
MCP_TOOL_ARNS: dict[str, str] = {
    "moogsoft": os.environ.get("MCP_MOOGSOFT_TOOL_ARN", ""),
    "splunk": os.environ.get("MCP_SPLUNK_TOOL_ARN", ""),
    "sysdig": os.environ.get("MCP_SYSDIG_TOOL_ARN", ""),
    "signalfx": os.environ.get("MCP_SIGNALFX_TOOL_ARN", ""),
    "dynatrace": os.environ.get("MCP_DYNATRACE_TOOL_ARN", ""),
}

# Retry / timeout config
MCP_CALL_TIMEOUT = int(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "30"))
MCP_MAX_RETRIES = int(os.environ.get("MCP_MAX_RETRIES", "2"))


def _has_any_arn() -> bool:
    """Check if any MCP tool ARN is configured."""
    return any(arn for arn in MCP_TOOL_ARNS.values())


# ---------------------------------------------------------------------------
# Boto3 client (lazy init)
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    """Lazily create the bedrock-agent-runtime boto3 client."""
    global _client
    if _client is not None:
        return _client
    if not _BOTO3_AVAILABLE:
        logger.debug("boto3 not installed — MCP gateway calls disabled")
        return None
    try:
        _client = boto3.client(
            "bedrock-agent-runtime",
            region_name=AWS_REGION,
            config=BotoConfig(
                retries={"max_attempts": MCP_MAX_RETRIES, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=MCP_CALL_TIMEOUT,
            ),
        )
        return _client
    except Exception as exc:
        logger.warning("Failed to create bedrock-agent-runtime client: %s", exc)
        return None


# ---------------------------------------------------------------------------
# MCP Tool name -> server mapping
# ---------------------------------------------------------------------------

_TOOL_TO_SERVER: dict[str, str] = {
    "moogsoft.get_incident_by_id": "moogsoft",
    "moogsoft.get_incidents": "moogsoft",
    "moogsoft.get_critical_incidents": "moogsoft",
    "moogsoft.get_alerts": "moogsoft",
    "moogsoft.get_historical_analysis": "moogsoft",
    "moogsoft.get_closed_incidents": "moogsoft",
    "splunk.search_oneshot": "splunk",
    "splunk.search_export": "splunk",
    "splunk.get_change_data": "splunk",
    "splunk.app_change_data": "splunk",
    "splunk.get_host_metrics": "splunk",
    "splunk.get_health_status": "splunk",
    "splunk.get_incident_data": "splunk",
    "sysdig.query_metrics": "sysdig",
    "sysdig.golden_signals": "sysdig",
    "sysdig.get_events": "sysdig",
    "sysdig.discover_resources": "sysdig",
    "sysdig.environment_status": "sysdig",
    "signalfx.query_signalfx_metrics": "signalfx",
    "signalfx.get_signalfx_active_incidents": "signalfx",
    "dynatrace.get_problems": "dynatrace",
    "dynatrace.get_metrics": "dynatrace",
    "dynatrace.get_entities": "dynatrace",
    "dynatrace.get_events": "dynatrace",
}


def get_server_for_tool(mcp_tool_name: str) -> str:
    """Map an MCP tool name to its server (moogsoft, splunk, sysdig, signalfx, dynatrace)."""
    return _TOOL_TO_SERVER.get(mcp_tool_name, "")


def get_arn_for_tool(mcp_tool_name: str) -> str:
    """Get the AgentCore tool ARN for an MCP tool name."""
    server = get_server_for_tool(mcp_tool_name)
    return MCP_TOOL_ARNS.get(server, "")


# ---------------------------------------------------------------------------
# Core: invoke MCP tool via AgentCore gateway
# ---------------------------------------------------------------------------

def invoke_mcp_tool(
    mcp_tool_name: str,
    tool_action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Invoke an MCP tool via AgentCore's tool invocation gateway.

    Args:
        mcp_tool_name: Fully qualified tool name (e.g. "splunk.search_oneshot")
        tool_action: The action/method on the MCP server
        params: Parameters to pass to the tool

    Returns:
        Response dict from the MCP tool, or error dict on failure.
        Falls back to empty stub responses when ARN not configured.
    """
    arn = get_arn_for_tool(mcp_tool_name)
    if not arn:
        logger.debug("No ARN configured for %s — returning stub", mcp_tool_name)
        return _stub_response(mcp_tool_name, tool_action, params)

    client = _get_client()
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

        # Parse the streaming response
        result = _parse_agent_response(response)

        logger.info(
            "MCP call: tool=%s action=%s elapsed=%.1fms",
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


# ---------------------------------------------------------------------------
# Stub responses (fallback when ARN not configured)
# ---------------------------------------------------------------------------

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

    return {}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def dispose() -> None:
    """Release the boto3 client."""
    global _client
    _client = None
