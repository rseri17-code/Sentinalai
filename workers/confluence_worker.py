"""Confluence Worker - handles documentation, runbook, and post-mortem lookups.

Calls MCP server via AgentCore gateway when configured.
Falls back to stub response for local dev / testing.

Provides:
- Runbook search: find operational runbooks for a service
- Post-mortem search: retrieve historical post-mortems by service/incident type
- Page retrieval: fetch a specific Confluence page by ID

Architecture:
    Agent -> AgentCore Gateway -> Confluence MCP Runtime Engine -> Confluence API

Phase placement:
    - search_runbooks:   Phase 1 (itsm_enrichment) — fetch runbooks alongside ITSM context
    - search_postmortems: Phase 1 (itsm_enrichment) — historical post-mortems for service
    - get_page:          On-demand (called when a specific page ID is known)
"""

from __future__ import annotations

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway


class ConfluenceWorker(BaseWorker):
    """Worker that interfaces with Confluence for documentation and runbooks."""

    worker_name = "confluence_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("search_runbooks", self._search_runbooks)
        self.register("search_postmortems", self._search_postmortems)
        self.register("get_page", self._get_page)

    def _search_runbooks(self, params: dict) -> dict:
        """Search Confluence for operational runbooks for a service.

        Params:
            service: Service name to look up runbooks for
            query: Optional additional search terms (e.g. incident type)

        Returns:
            {"runbooks": [{"title", "url", "space", "last_updated", "excerpt"}]}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "confluence.search_runbooks",
            "search_runbooks",
            {
                "service": service,
                "query": params.get("query", ""),
            },
        )

    def _search_postmortems(self, params: dict) -> dict:
        """Search Confluence for post-mortems related to a service or incident type.

        Params:
            service: Service name
            incident_type: Optional incident classification (e.g. "latency", "error_spike")
            time_window_days: Lookback window in days (default 180)

        Returns:
            {"postmortems": [{"title", "url", "space", "date", "root_cause_summary",
                              "action_items": [...]}]}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "confluence.search_postmortems",
            "search_postmortems",
            {
                "service": service,
                "incident_type": params.get("incident_type", ""),
                "time_window_days": params.get("time_window_days", 180),
            },
        )

    def _get_page(self, params: dict) -> dict:
        """Retrieve a specific Confluence page by ID.

        Params:
            page_id: Confluence page ID

        Returns:
            {"page": {"id", "title", "url", "space", "body", "last_updated", "author"}}
        """
        page_id = params.get("page_id", "")
        if not page_id:
            return {"error": "page_id required"}
        return self._gateway.invoke(
            "confluence.get_page",
            "get_page",
            {"page_id": page_id},
        )
