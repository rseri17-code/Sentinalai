"""Minimal JSON-RPC Streamable HTTP MCP (the transport ``McpGateway`` uses).

Implements the subset ``mcp.client.streamable_http.streamablehttp_client``
needs: ``initialize``, ``notifications/initialized``, ``tools/list``,
``tools/call``, ``ping``. Responses are JSON by default; SSE is used when
the client only accepts ``text/event-stream``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from oss_validation_gateway import __version__
from oss_validation_gateway.names import tool_catalog

SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

DispatchFn = Callable[[str, dict[str, Any] | None], dict[str, Any]]


def _protocol_version(requested: str | None) -> str:
    if requested and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


def handle_rpc(
    message: dict[str, Any],
    *,
    dispatch: DispatchFn,
    session_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Handle one JSON-RPC message.

    Returns ``(response_or_None, session_id)``. Notifications yield ``None``.
    """
    sid = session_id or str(uuid.uuid4())
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None, sid

    if method == "initialize":
        requested = ""
        if isinstance(params, dict):
            requested = str(params.get("protocolVersion") or "")
        result = {
            "protocolVersion": _protocol_version(requested),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "sentinal-oss-validation",
                "version": __version__,
            },
        }
        return _ok(msg_id, result), sid

    if method == "ping":
        return _ok(msg_id, {}), sid

    if method == "tools/list":
        return _ok(msg_id, {"tools": tool_catalog()}), sid

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        payload = dispatch(name, arguments)
        text = json.dumps(payload, default=str)
        is_error = bool(isinstance(payload, dict) and payload.get("error") and payload.get("incident") is None and "logs" not in payload and "metrics" not in payload and "signals" not in payload and not payload.get("skipped"))
        # Unknown tool is an error; backend misses with stub-compatible keys are not.
        if isinstance(payload, dict) and payload.get("error") == "unknown_tool":
            is_error = True
        result = {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }
        return _ok(msg_id, result), sid

    if msg_id is None:
        return None, sid
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }, sid


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def encode_sse(payload: dict[str, Any]) -> str:
    return f"event: message\ndata: {json.dumps(payload, default=str)}\n\n"
