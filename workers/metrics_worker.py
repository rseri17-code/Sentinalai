"""Metrics Worker - handles Sysdig metrics operations.

Calls MCP server via AgentCore gateway when configured.
Falls back to stub response for local dev / testing.
"""

from __future__ import annotations

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway


class MetricsWorker(BaseWorker):
    """Worker that interfaces with Sysdig for metrics and events."""

    worker_name = "metrics_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("query_metrics", self._query_metrics)
        self.register("get_resource_metrics", self._query_metrics)
        self.register("get_events", self._get_events)

    def _query_metrics(self, params: dict) -> dict:
        """Query time-series metrics from Sysdig via AgentCore gateway."""
        return self._gateway.invoke(
            "sysdig.query_metrics",
            "query_metrics",
            params,
        )

    def _get_events(self, params: dict) -> dict:
        """Get infrastructure events from Sysdig."""
        return self._gateway.invoke(
            "sysdig.get_events",
            "get_events",
            params,
        )
