"""Ops Worker - handles Moogsoft incident operations.

In production this calls MCP server at localhost:5001.
For testing, the execute() method is monkey-patched with mocks.
"""

from workers.base_worker import BaseWorker


class OpsWorker(BaseWorker):
    """Worker that interfaces with Moogsoft for incident data."""

    def __init__(self):
        super().__init__()
        self.register("get_incident_by_id", self._get_incident_by_id)

    def _get_incident_by_id(self, params: dict) -> dict:
        """Fetch incident details from Moogsoft."""
        incident_id = params.get("incident_id") or params.get("id")
        if not incident_id:
            return {"error": "incident_id required"}
        return {"incident": {"incident_id": incident_id, "status": "pending"}}
