"""Log Worker - handles Splunk log operations.

In production this calls MCP server at localhost:5002.
For testing, the execute() method is monkey-patched with mocks.
"""

from workers.base_worker import BaseWorker


class LogWorker(BaseWorker):
    """Worker that interfaces with Splunk for log search and change data."""

    def __init__(self):
        super().__init__()
        self.register("search_logs", self._search_logs)
        self.register("get_change_data", self._get_change_data)

    def _search_logs(self, params: dict) -> dict:
        """Search Splunk logs."""
        query = params.get("query", "")
        if not query:
            return {"logs": {"results": [], "count": 0}}
        return {"logs": {"results": [], "count": 0}}

    def _get_change_data(self, params: dict) -> dict:
        """Retrieve recent change/deployment data from Splunk."""
        return {"changes": []}
