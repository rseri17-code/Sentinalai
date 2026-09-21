"""AgentCore-style tool names for the OSS validation gateway.

``McpGateway._to_gateway_tool_name`` rewrites dotted worker names such as
``splunk.search_oneshot`` to ``{Target}___{operation}`` using
``AGENTCORE_TARGET_*`` (defaults in ``workers/mcp_client.py``). This module
mirrors those defaults and accepts both naming styles on the way in.
"""

from __future__ import annotations

import os
from typing import Any

# Must match workers/mcp_client.py _SERVER_TO_TARGET defaults.
DEFAULT_TARGETS: dict[str, str] = {
    "moogsoft": "MoogsoftTarget",
    "splunk": "SplunkTarget",
    "sysdig": "SysdigTarget",
    "signalfx": "SignalFxTarget",
    "dynatrace": "DynatraceTarget",
    "servicenow": "ServiceNowTarget",
    "github": "GitHubTarget",
    "confluence": "ConfluenceTarget",
    "kubernetes": "KubernetesTarget",
}

_TARGET_ENV: dict[str, str] = {
    "moogsoft": "AGENTCORE_TARGET_MOOGSOFT",
    "splunk": "AGENTCORE_TARGET_SPLUNK",
    "sysdig": "AGENTCORE_TARGET_SYSDIG",
    "signalfx": "AGENTCORE_TARGET_SIGNALFX",
    "dynatrace": "AGENTCORE_TARGET_DYNATRACE",
    "servicenow": "AGENTCORE_TARGET_SERVICENOW",
    "github": "AGENTCORE_TARGET_GITHUB",
    "confluence": "AGENTCORE_TARGET_CONFLUENCE",
    "kubernetes": "AGENTCORE_TARGET_KUBERNETES",
}

# server → operations the shim implements (or honestly skips).
# Keep in sync with workers/mcp_client.py _TOOL_TO_SERVER plus a few extras
# that workers still invoke (sysdig.get_kubernetes_events).
SERVER_OPERATIONS: dict[str, tuple[str, ...]] = {
    "moogsoft": (
        "get_incident_by_id",
        "get_incidents",
        "get_critical_incidents",
        "get_alerts",
        "get_historical_analysis",
        "get_closed_incidents",
    ),
    "splunk": (
        "search_oneshot",
        "search_export",
        "get_change_data",
        "app_change_data",
        "get_host_metrics",
        "get_health_status",
        "get_incident_data",
    ),
    "sysdig": (
        "query_metrics",
        "golden_signals",
        "get_events",
        "discover_resources",
        "environment_status",
        "get_kubernetes_events",
    ),
    "signalfx": (
        "query_signalfx_metrics",
        "get_signalfx_active_incidents",
    ),
    "dynatrace": (
        "get_problems",
        "get_metrics",
        "get_entities",
        "get_events",
    ),
    "servicenow": (
        "get_ci_details",
        "search_incidents",
        "get_change_records",
        "get_known_errors",
    ),
    "github": (
        "get_recent_deployments",
        "get_pr_details",
        "get_commit_diff",
        "get_workflow_runs",
        "create_fix_pr",
        "create_pull_request",
        "reply_to_review_comment",
        "request_reviewers",
    ),
    "confluence": (
        "search_runbooks",
        "search_postmortems",
        "get_page",
    ),
    "kubernetes": (
        "rollback_deployment",
        "scale_service",
        "get_deployment_status",
        "get_pod_logs",
    ),
}

_GENERIC_INPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}

_TOOL_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("moogsoft", "get_incident_by_id"): "Fetch one firing Alertmanager alert as a Moogsoft-shaped incident.",
    ("moogsoft", "get_incidents"): "List firing Alertmanager alerts as Moogsoft-shaped incidents.",
    ("moogsoft", "get_alerts"): "List Alertmanager alerts.",
    ("splunk", "search_oneshot"): "Search Loki logs (LogQL) and return Splunk-shaped results.",
    ("splunk", "search_export"): "Search Loki logs (LogQL) and return Splunk-shaped results.",
    ("splunk", "get_change_data"): "Change data is not available from Loki; returns an empty list.",
    ("sysdig", "query_metrics"): "Query Prometheus (PromQL) and return Sysdig-shaped metrics.",
    ("sysdig", "golden_signals"): "Golden signals from Prometheus demo/latency metrics.",
    ("sysdig", "get_events"): "Infrastructure events from Alertmanager.",
    ("dynatrace", "get_metrics"): "Golden signals from Prometheus (APM stand-in).",
    ("dynatrace", "get_problems"): "Alertmanager alerts as Dynatrace-shaped problems.",
    ("signalfx", "query_signalfx_metrics"): "Golden signals from Prometheus (APM enrichment).",
    ("kubernetes", "get_deployment_status"): "Read-only Kubernetes deployment status.",
    ("kubernetes", "get_pod_logs"): "Read-only Kubernetes pod logs.",
}


def target_for_server(server: str) -> str:
    """Return the AgentCore target string for a logical MCP server."""
    env_name = _TARGET_ENV.get(server, "")
    if env_name:
        override = os.environ.get(env_name, "").strip()
        if override:
            return override
    return DEFAULT_TARGETS.get(server, "")


def targets_by_server() -> dict[str, str]:
    return {server: target_for_server(server) for server in DEFAULT_TARGETS}


def gateway_tool_name(server: str, operation: str) -> str:
    target = target_for_server(server)
    if not target:
        return f"{server}.{operation}"
    return f"{target}___{operation}"


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """Parse a gateway or dotted tool name into ``(server, operation)``.

    Accepts:
      - ``MoogsoftTarget___get_incident_by_id``
      - ``moogsoft.get_incident_by_id``
      - env-overridden target strings from ``AGENTCORE_TARGET_*``
    """
    raw = (name or "").strip()
    if not raw:
        return None

    if "___" in raw:
        target, operation = raw.split("___", 1)
        target, operation = target.strip(), operation.strip()
        if not target or not operation:
            return None
        mapping = {target_for_server(s): s for s in DEFAULT_TARGETS}
        server = mapping.get(target)
        if server is None:
            # Tolerate "MoogsoftTarget" vs "moogsofttarget" and a trailing "Target".
            lowered = {k.lower(): v for k, v in mapping.items()}
            server = lowered.get(target.lower())
        if server is None:
            stripped = target.lower().removesuffix("target")
            for candidate in DEFAULT_TARGETS:
                if candidate == stripped:
                    server = candidate
                    break
        if server is None:
            return None
        return server, operation

    if "." in raw:
        server, operation = raw.split(".", 1)
        server, operation = server.strip().lower(), operation.strip()
        if server in DEFAULT_TARGETS and operation:
            return server, operation

    return None


def tool_catalog() -> list[dict[str, Any]]:
    """MCP ``tools/list`` catalog using AgentCore triple-underscore names."""
    tools: list[dict[str, Any]] = []
    for server, operations in SERVER_OPERATIONS.items():
        for operation in operations:
            tools.append({
                "name": gateway_tool_name(server, operation),
                "description": _TOOL_DESCRIPTIONS.get(
                    (server, operation),
                    f"OSS validation shim for {server}.{operation}",
                ),
                "inputSchema": _GENERIC_INPUT,
            })
    return tools
