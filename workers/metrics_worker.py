"""Metrics Worker - handles Sysdig metrics operations.

Calls MCP server via AgentCore tool ARN when MCP_SYSDIG_TOOL_ARN is set.
Falls back to stub response for local dev / testing.
"""

from workers.base_worker import BaseWorker
from workers.mcp_client import invoke_mcp_tool


class MetricsWorker(BaseWorker):
    """Worker that interfaces with Sysdig for metrics and events."""

    worker_name = "metrics_worker"

    def __init__(self):
        super().__init__()
        self.register("query_metrics", self._query_metrics)
        self.register("get_resource_metrics", self._query_metrics)
        self.register("get_events", self._get_events)

    def _query_metrics(self, params: dict) -> dict:
        """Query time-series metrics from Sysdig via MCP ARN."""
        return invoke_mcp_tool(
            "sysdig.query_metrics",
            "query_metrics",
            params,
        )

    def _get_events(self, params: dict) -> dict:
        """Get infrastructure events from Sysdig via MCP ARN."""
        return invoke_mcp_tool(
            "sysdig.get_events",
            "get_events",
            params,
        )
