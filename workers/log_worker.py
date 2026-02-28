"""Log Worker - handles Splunk log operations.

Calls MCP server via AgentCore gateway when configured.
Falls back to stub response for local dev / testing.
"""

from __future__ import annotations

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway


class LogWorker(BaseWorker):
    """Worker that interfaces with Splunk for log search and change data."""

    worker_name = "log_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("search_logs", self._search_logs)
        self.register("get_change_data", self._get_change_data)

    def _search_logs(self, params: dict) -> dict:
        """Search Splunk logs via AgentCore gateway."""
        return self._gateway.invoke(
            "splunk.search_oneshot",
            "search_logs",
            params,
        )

    def _get_change_data(self, params: dict) -> dict:
        """Retrieve recent change/deployment data from Splunk."""
        return self._gateway.invoke(
            "splunk.get_change_data",
            "get_change_data",
            params,
        )
