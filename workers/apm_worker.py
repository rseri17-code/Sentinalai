"""APM Worker - handles Sysdig APM / golden signals operations.

Calls MCP server via AgentCore tool ARN when MCP_SYSDIG_TOOL_ARN is set.
Falls back to stub response for local dev / testing.
"""

from workers.base_worker import BaseWorker
from workers.mcp_client import invoke_mcp_tool


class ApmWorker(BaseWorker):
    """Worker that interfaces with Sysdig for golden signals / APM data."""

    worker_name = "apm_worker"

    def __init__(self):
        super().__init__()
        self.register("get_golden_signals", self._get_golden_signals)
        self.register("check_latency", self._get_golden_signals)

    def _get_golden_signals(self, params: dict) -> dict:
        """Get golden signals (latency, traffic, errors, saturation) via MCP ARN."""
        return invoke_mcp_tool(
            "sysdig.golden_signals",
            "get_golden_signals",
            params,
        )
