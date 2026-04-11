"""ITSM Worker - handles ServiceNow CMDB and change management operations.

Calls MCP server via AgentCore gateway when configured.
Falls back to stub response for local dev / testing.

Provides:
- CI (Configuration Item) lookup: service tier, dependencies, owner, SLA
- Change record retrieval: who, when, what was approved, rollback plan
- Similar/recent incident search for a given CI
- Known error lookup: existing workarounds for known problems

Architecture:
    Agent -> AgentCore Gateway -> ServiceNow MCP Runtime Engine -> ServiceNow API

Phase placement:
    - get_ci_details:        Phase 1 (initial_context) — hydrate CI metadata
    - search_incidents:      Phase 1 (initial_context) — find similar/recent incidents
    - get_known_errors:      Phase 1 (initial_context) — check for known workarounds
    - get_change_records:    Phase 3 (change_correlation) — ITSM change records
"""

from __future__ import annotations

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway


class ItsmWorker(BaseWorker):
    """Worker that interfaces with ServiceNow for CMDB and change management."""

    worker_name = "itsm_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("get_ci_details", self._get_ci_details)
        self.register("search_incidents", self._search_incidents)
        self.register("get_change_records", self._get_change_records)
        self.register("get_known_errors", self._get_known_errors)
        self.register("update_incident", self._update_incident)

    def _get_ci_details(self, params: dict) -> dict:
        """Retrieve Configuration Item details from ServiceNow CMDB.

        Params:
            service: Service name to look up in CMDB

        Returns:
            {"ci": {"name", "sys_class_name", "tier", "owner",
                    "dependencies", "sla", "environment"}}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "servicenow.get_ci_details",
            "get_ci_details",
            {"service": service},
        )

    def _search_incidents(self, params: dict) -> dict:
        """Search ServiceNow for similar or recent incidents on a CI.

        Params:
            service: Service / CI name
            query: Optional natural-language description to match against
            time_window_hours: Lookback window (default 72)

        Returns:
            {"incidents": [{"number", "short_description", "state",
                            "priority", "resolved_at", "root_cause"}]}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "servicenow.search_incidents",
            "search_incidents",
            {
                "service": service,
                "query": params.get("query", ""),
                "time_window_hours": params.get("time_window_hours", 72),
            },
        )

    def _get_change_records(self, params: dict) -> dict:
        """Retrieve change records from ServiceNow for a given service.

        Richer than splunk.get_change_data: includes approval chain,
        rollback plan, CI impact, and risk assessment.

        Params:
            service: Service / CI name
            time_window_hours: Lookback window (default 24)

        Returns:
            {"change_records": [{"number", "type", "short_description",
                                 "state", "start_date", "end_date",
                                 "requested_by", "approval", "risk",
                                 "rollback_plan", "ci_impact"}]}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "servicenow.get_change_records",
            "get_change_records",
            {
                "service": service,
                "time_window_hours": params.get("time_window_hours", 24),
            },
        )

    def _get_known_errors(self, params: dict) -> dict:
        """Check ServiceNow Known Error Database (KEDB) for a service.

        Params:
            service: Service / CI name
            summary: Optional incident summary for matching

        Returns:
            {"known_errors": [{"number", "short_description",
                               "workaround", "related_problem"}]}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "servicenow.get_known_errors",
            "get_known_errors",
            {
                "service": service,
                "summary": params.get("summary", ""),
            },
        )

    def _update_incident(self, params: dict) -> dict:
        """Update a ServiceNow incident (state, resolution, assignment).

        Used for:
        - Auto-close after successful fix verification (state=resolved)
        - Update work notes during investigation
        - Escalate severity when CI analysis reveals blast radius

        Params:
            incident_id:       ServiceNow incident number (e.g. INC0012345)
            state:             Target state: open | in_progress | resolved | closed
            resolution_code:   Optional: Solved (Permanently) | Solved (Workaround) etc.
            resolution_notes:  Notes explaining how it was resolved
            work_notes:        Internal work notes (not visible to reporter)
            assigned_to:       Optional re-assignment
            priority:          Optional priority change (1=Critical .. 5=Planning)
            close_code:        Optional close code for closed state

        Returns:
            {"updated": {"number", "state", "sys_updated_on", "resolved_at"}}
        """
        incident_id = params.get("incident_id", "")
        if not incident_id:
            return {"error": "incident_id required"}
        return self._gateway.invoke(
            "servicenow.update_incident",
            "update_incident",
            {
                "incident_id": incident_id,
                "state": params.get("state", ""),
                "resolution_code": params.get("resolution_code", ""),
                "resolution_notes": params.get("resolution_notes", ""),
                "work_notes": params.get("work_notes", ""),
                "assigned_to": params.get("assigned_to", ""),
                "priority": params.get("priority", ""),
                "close_code": params.get("close_code", ""),
            },
        )
