"""Metrics Worker - handles Sysdig metrics operations.

In production this calls MCP server at localhost:5003.
For testing, the execute() method is monkey-patched with mocks.
"""

from workers.base_worker import BaseWorker


class MetricsWorker(BaseWorker):
    """Worker that interfaces with Sysdig for metrics and events."""

    def __init__(self):
        super().__init__()
        self.register("query_metrics", self._query_metrics)
        self.register("get_resource_metrics", self._query_metrics)
        self.register("get_events", self._get_events)

    def _query_metrics(self, params: dict) -> dict:
        """Query time-series metrics from Sysdig."""
        return {"metrics": {"metrics": [], "baseline": 0}}

    def _get_events(self, params: dict) -> dict:
        """Get infrastructure events from Sysdig."""
        return {"events": []}
