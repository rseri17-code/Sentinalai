"""Log Worker - handles Splunk log operations.

Calls MCP server via AgentCore gateway when configured.
Falls back to stub response for local dev / testing.
"""

from __future__ import annotations

import logging

from supervisor.guardrails import validate_query
from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway

logger = logging.getLogger(__name__)


class LogWorker(BaseWorker):
    """Worker that interfaces with Splunk for log search and change data."""

    worker_name = "log_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("search_logs", self._search_logs)
        self.register("get_change_data", self._get_change_data)
        # Aliases for incident-type-specific actions referenced by splunk_retrieval_planner.
        # All delegate to _search_logs — the incident-specific query string is in params.
        self.register("search_oom_logs",        self._search_logs)
        self.register("search_timeout_logs",    self._search_logs)
        self.register("search_error_logs",      self._search_logs)
        self.register("search_saturation_logs", self._search_logs)
        self.register("get_error_logs",         self._search_logs)

    def _search_logs(self, params: dict) -> dict:
        """Search Splunk logs via AgentCore gateway.

        Validates the query against the policy allowlist before dispatching
        to the MCP gateway (remediation for G1.1 — validate_query was dead code).
        """
        query = params.get("query", "")
        if query:
            is_valid, reason = validate_query(query)
            if not is_valid:
                logger.warning("Query rejected by policy: %s (query=%r)", reason, query[:120])
                return {"error": f"query_rejected: {reason}", "logs": {"results": [], "count": 0}}

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
