"""APM Worker - handles Sysdig APM / golden signals operations.

In production this calls MCP server at localhost:5003.
For testing, the execute() method is monkey-patched with mocks.
"""

from workers.base_worker import BaseWorker


class ApmWorker(BaseWorker):
    """Worker that interfaces with Sysdig for golden signals / APM data."""

    worker_name = "apm_worker"

    def __init__(self):
        super().__init__()
        self.register("get_golden_signals", self._get_golden_signals)
        self.register("check_latency", self._get_golden_signals)

    def _get_golden_signals(self, params: dict) -> dict:
        """Get golden signals (latency, traffic, errors, saturation) for a service."""
        return {"signals": {}}
