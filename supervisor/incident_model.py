"""Canonical incident model for SentinalAI.

Normalizes incident data from multiple sources (Moogsoft, ServiceNow,
PagerDuty, manual trigger) into a common schema with validation.

Usage:
    from supervisor.incident_model import Incident

    incident = Incident.from_moogsoft(raw_moogsoft_data)
    incident = Incident.from_servicenow(raw_snow_data)
    incident = Incident.from_pagerduty(raw_pd_data)
    incident = Incident.from_dict(raw_dict)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.incident_model")


@dataclass
class Incident:
    """Canonical normalized incident model.

    All intake sources are normalized to this schema before investigation.
    """

    incident_id: str
    summary: str = ""
    affected_service: str = "unknown"
    severity: int = 3  # 1 (critical) to 5 (info)
    severity_label: str = "medium"
    source: str = "unknown"  # moogsoft, servicenow, pagerduty, manual
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""
    assigned_to: str = ""
    priority: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Validate and normalize fields after initialization."""
        if not self.incident_id:
            raise ValueError("incident_id is required")
        # Ensure severity is in range
        if not isinstance(self.severity, int) or self.severity < 1 or self.severity > 5:
            self.severity = 3
        # Set severity label
        self.severity_label = _SEVERITY_LABELS.get(self.severity, "medium")
        # Default summary from description if empty
        if not self.summary and self.description:
            self.summary = self.description[:200]
        # Default created_at to now
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (compatible with existing agent.py expectations)."""
        d = asdict(self)
        d.pop("raw_data", None)
        return d

    def to_legacy_dict(self) -> dict[str, Any]:
        """Return dict matching the legacy format expected by agent.py.

        This bridges the canonical model back to the raw dict format
        that _fetch_incident returns, so existing code continues to work.
        """
        return {
            "incident_id": self.incident_id,
            "summary": self.summary,
            "affected_service": self.affected_service,
            "severity": self.severity,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "assigned_to": self.assigned_to,
            "priority": self.priority,
            "description": self.description,
            "tags": self.tags,
        }

    # ------------------------------------------------------------------ #
    # Factory methods for different sources
    # ------------------------------------------------------------------ #

    @classmethod
    def from_moogsoft(cls, data: dict) -> Incident:
        """Normalize a Moogsoft incident to canonical model.

        Moogsoft fields:
            incident_id, summary/description, affected_service/service,
            severity (1-5 or string), status, created_at
        """
        severity_raw = data.get("severity")
        severity = _normalize_severity(severity_raw)

        return cls(
            incident_id=str(data.get("incident_id", data.get("id", ""))),
            summary=data.get("summary", data.get("description", "")),
            affected_service=data.get("affected_service", data.get("service", "unknown")),
            severity=severity,
            source="moogsoft",
            status=data.get("status", "open"),
            created_at=data.get("created_at", data.get("createdAt", "")),
            updated_at=data.get("updated_at", data.get("updatedAt", "")),
            assigned_to=data.get("assigned_to", data.get("assignee", "")),
            priority=str(data.get("priority", "")),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            raw_data=data,
        )

    @classmethod
    def from_servicenow(cls, data: dict) -> Incident:
        """Normalize a ServiceNow incident to canonical model.

        ServiceNow fields:
            number, short_description, cmdb_ci/service, priority,
            state, sys_created_on, assigned_to
        """
        # ServiceNow priority: 1=Critical, 2=High, 3=Moderate, 4=Low
        priority = data.get("priority", 3)
        severity = min(5, max(1, int(priority))) if isinstance(priority, (int, float)) else 3

        return cls(
            incident_id=data.get("number", data.get("sys_id", "")),
            summary=data.get("short_description", ""),
            affected_service=data.get("cmdb_ci", data.get("service", "unknown")),
            severity=severity,
            source="servicenow",
            status=_normalize_snow_state(data.get("state", "")),
            created_at=data.get("sys_created_on", data.get("opened_at", "")),
            updated_at=data.get("sys_updated_on", ""),
            assigned_to=data.get("assigned_to", ""),
            priority=str(data.get("priority", "")),
            description=data.get("description", data.get("short_description", "")),
            tags=data.get("tags", []),
            raw_data=data,
        )

    @classmethod
    def from_pagerduty(cls, data: dict) -> Incident:
        """Normalize a PagerDuty incident to canonical model.

        PagerDuty fields:
            id, title, service.summary, urgency (high/low),
            status (triggered/acknowledged/resolved), created_at
        """
        urgency = data.get("urgency", "low")
        severity = 2 if urgency == "high" else 4

        service_data = data.get("service", {})
        service_name = (
            service_data.get("summary", service_data.get("name", "unknown"))
            if isinstance(service_data, dict) else str(service_data)
        )

        return cls(
            incident_id=data.get("id", data.get("incident_number", "")),
            summary=data.get("title", data.get("description", "")),
            affected_service=service_name,
            severity=severity,
            source="pagerduty",
            status=data.get("status", "triggered"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", data.get("last_status_change_at", "")),
            assigned_to=_extract_pd_assignee(data),
            priority=data.get("urgency", ""),
            description=data.get("description", data.get("title", "")),
            tags=data.get("tags", []),
            raw_data=data,
        )

    @classmethod
    def from_dict(cls, data: dict) -> Incident:
        """Normalize a generic dict to canonical model.

        Auto-detects source based on field presence and delegates.
        Falls back to direct field mapping for manual triggers.
        """
        if not data:
            raise ValueError("Empty incident data")

        # Auto-detect source
        if "incident_id" in data and "summary" in data:
            # Has Moogsoft-style fields; check explicit source overrides
            if data.get("source") == "servicenow":
                return cls.from_servicenow(data)
            if data.get("source") == "pagerduty" or "urgency" in data:
                return cls.from_pagerduty(data)
            # Default to Moogsoft when incident_id + summary present
            return cls.from_moogsoft(data)
        if "number" in data and "short_description" in data:
            return cls.from_servicenow(data)
        if "title" in data and "urgency" in data:
            return cls.from_pagerduty(data)

        # Fallback: manual/generic
        return cls(
            incident_id=str(
                data.get("incident_id", data.get("id", data.get("number", "")))
            ),
            summary=data.get("summary", data.get("title", data.get("short_description", ""))),
            affected_service=data.get(
                "affected_service", data.get("service", data.get("cmdb_ci", "unknown"))
            ),
            severity=_normalize_severity(data.get("severity", 3)),
            source=data.get("source", "manual"),
            status=data.get("status", "open"),
            description=data.get("description", ""),
            raw_data=data,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEVERITY_LABELS = {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "info"}

_MOOGSOFT_STRING_MAP = {
    "critical": 1,
    "major": 2,
    "warning": 3,
    "minor": 4,
    "info": 5,
}


def _normalize_severity(value: Any) -> int:
    """Normalize a severity value from any source to 1-5."""
    if value is None:
        return 3
    if isinstance(value, int):
        return max(1, min(5, value))
    if isinstance(value, float):
        return max(1, min(5, int(value)))
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _MOOGSOFT_STRING_MAP:
            return _MOOGSOFT_STRING_MAP[cleaned]
        try:
            return max(1, min(5, int(cleaned)))
        except ValueError:
            return 3
    return 3


def _normalize_snow_state(state: Any) -> str:
    """Normalize ServiceNow state to a simple status string."""
    state_map = {
        "1": "new", "2": "in_progress", "3": "on_hold",
        "6": "resolved", "7": "closed", "8": "cancelled",
    }
    state_str = str(state).strip()
    return state_map.get(state_str, state_str.lower() if isinstance(state, str) else "open")


def _extract_pd_assignee(data: dict) -> str:
    """Extract assignee from PagerDuty assignments."""
    assignments = data.get("assignments", [])
    if assignments and isinstance(assignments, list):
        first = assignments[0]
        if isinstance(first, dict):
            assignee = first.get("assignee", {})
            if isinstance(assignee, dict):
                return assignee.get("summary", assignee.get("name", ""))
    return ""
