"""Ops Worker - handles Moogsoft incident operations.

Calls MCP server via AgentCore tool ARN when MCP_MOOGSOFT_TOOL_ARN is set.
Falls back to stub response for local dev / testing.
"""

from workers.base_worker import BaseWorker
from workers.mcp_client import invoke_mcp_tool, get_arn_for_tool


class OpsWorker(BaseWorker):
    """Worker that interfaces with Moogsoft for incident data."""

    worker_name = "ops_worker"

    def __init__(self):
        super().__init__()
        self.register("get_incident_by_id", self._get_incident_by_id)

    def _get_incident_by_id(self, params: dict) -> dict:
        """Fetch incident details from Moogsoft via MCP ARN."""
        incident_id = params.get("incident_id") or params.get("id")
        if not incident_id:
            return {"error": "incident_id required"}
        return invoke_mcp_tool(
            "moogsoft.get_incident_by_id",
            "get_incident_by_id",
            {"incident_id": incident_id},
        )
