"""Log Worker - handles Splunk log operations.

Calls MCP server via AgentCore tool ARN when MCP_SPLUNK_TOOL_ARN is set.
Falls back to stub response for local dev / testing.
"""

from workers.base_worker import BaseWorker
from workers.mcp_client import invoke_mcp_tool


class LogWorker(BaseWorker):
    """Worker that interfaces with Splunk for log search and change data."""

    worker_name = "log_worker"

    def __init__(self):
        super().__init__()
        self.register("search_logs", self._search_logs)
        self.register("get_change_data", self._get_change_data)

    def _search_logs(self, params: dict) -> dict:
        """Search Splunk logs via MCP ARN."""
        return invoke_mcp_tool(
            "splunk.search_oneshot",
            "search_logs",
            params,
        )

    def _get_change_data(self, params: dict) -> dict:
        """Retrieve recent change/deployment data from Splunk via MCP ARN."""
        return invoke_mcp_tool(
            "splunk.get_change_data",
            "get_change_data",
            params,
        )
