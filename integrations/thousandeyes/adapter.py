"""ThousandEyes MCP adapter.

Makes read-only calls to the ThousandEyes MCP server (HTTP transport on
TE_MCP_URL, default http://localhost:8004).  Auth via TE_TOKEN Bearer header.

When TE_USE_FIXTURES=true, all calls return sanitized fixture data — no live
TE account or MCP server required.  This is the default for CI.

All tools exposed here are read-only:
  - te_list_alerts    (P0: alert correlation)
  - te_get_test_results (P0: per-agent metrics)
  - te_list_tests     (P1: test catalog)
  - te_list_agents    (P2: agent metadata)

te_get_users is intentionally excluded (PII risk).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from integrations.thousandeyes import fixture_loader

logger = logging.getLogger(__name__)

TE_MCP_URL = os.environ.get("TE_MCP_URL", "http://localhost:8004")
TE_TIMEOUT = int(os.environ.get("TE_TIMEOUT", "10"))

# Rate limit: 240 req/min → 4 req/s.  Track last call time for simple throttle.
_RATE_LIMIT_MIN_INTERVAL = 0.25  # seconds between calls

_last_call_time: float = 0.0

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _requests = None  # type: ignore[assignment]
    _REQUESTS_AVAILABLE = False


def _get_token() -> str:
    return os.environ.get("TE_TOKEN", "")


def _call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST a tool call to the ThousandEyes MCP server.

    Returns the parsed response dict, or an error dict on failure.
    Never logs the token value.
    """
    global _last_call_time

    if fixture_loader.fixture_mode_enabled():
        return fixture_loader.load(tool)

    token = _get_token()
    if not token:
        logger.error("TE_TOKEN not set — ThousandEyes calls will fail")
        return {"error": "missing_token", "tool": tool}

    if not _REQUESTS_AVAILABLE:
        logger.warning("requests library not installed — cannot call ThousandEyes MCP")
        return {"error": "requests_unavailable", "tool": tool}

    # Simple rate throttle
    now = time.monotonic()
    gap = now - _last_call_time
    if gap < _RATE_LIMIT_MIN_INTERVAL:
        time.sleep(_RATE_LIMIT_MIN_INTERVAL - gap)
    _last_call_time = time.monotonic()

    url = f"{TE_MCP_URL.rstrip('/')}/mcp"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {"tool": tool, "arguments": arguments or {}}

    try:
        resp = _requests.post(url, headers=headers, json=body, timeout=TE_TIMEOUT)
    except Exception as exc:
        logger.error("ThousandEyes MCP call failed (tool=%s): %s", tool, exc)
        return {"error": f"connection_error: {exc}", "tool": tool}

    if resp.status_code == 401:
        logger.error("ThousandEyes auth failed (401) — check TE_TOKEN")
        return {"error": "unauthorized", "tool": tool}
    if resp.status_code == 403:
        logger.error("ThousandEyes permission denied (403) for tool=%s", tool)
        return {"error": "forbidden", "tool": tool}
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "15"))
        logger.warning("ThousandEyes rate limited (429) — retry after %ds", retry_after)
        time.sleep(min(retry_after, 15))
        # Single retry
        try:
            resp = _requests.post(url, headers=headers, json=body, timeout=TE_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            return {"error": f"rate_limit_retry_failed: {exc}", "tool": tool}
    elif not resp.ok:
        logger.error("ThousandEyes MCP error: status=%d tool=%s", resp.status_code, tool)
        return {"error": f"http_{resp.status_code}", "tool": tool}

    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("ThousandEyes MCP response parse error (tool=%s): %s", tool, exc)
        return {"error": f"parse_error: {exc}", "tool": tool}


def list_alerts(window_start: str | None = None, window_end: str | None = None) -> dict:
    """Return active ThousandEyes alerts, optionally filtered by time window."""
    args: dict[str, Any] = {}
    if window_start:
        args["window_start"] = window_start
    if window_end:
        args["window_end"] = window_end
    return _call("te_list_alerts", args)


def get_test_results(test_id: int | str, window_start: str | None = None) -> dict:
    """Return per-agent test results for one test."""
    args: dict[str, Any] = {"test_id": test_id}
    if window_start:
        args["window_start"] = window_start
    return _call("te_get_test_results", args)


def list_tests() -> dict:
    """Return the catalog of configured ThousandEyes tests."""
    return _call("te_list_tests")


def list_agents() -> dict:
    """Return all ThousandEyes agents (cloud, enterprise, endpoint)."""
    return _call("te_list_agents")
