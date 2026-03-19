"""AG UI Incident State Schema v1.0

Incident state is the top-level aggregate for the AG UI.
It captures the full lifecycle: intake → investigation → RCA → remediation.

This schema is the source of truth for DynamoDB state store.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


CURRENT_INCIDENT_SCHEMA_VERSION = "1.0"


class InvestigationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    REPLAYING = "replaying"


class IncidentSeverity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    WARNING = "warning"
    MINOR = "minor"
    INFO = "info"


class ControlActionType(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    PAUSE = "pause"
    RESUME = "resume"
    OVERRIDE = "override"
    ESCALATE = "escalate"


class ControlAction(BaseModel):
    """Human-in-the-loop control action — immutable audit record."""
    model_config = {"extra": "allow"}

    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    investigation_id: str
    incident_id: str
    action_type: ControlActionType
    actor_id: str            # JWT sub claim
    actor_role: str          # viewer | operator | approver | admin
    reason: Optional[str] = None
    target_node_id: Optional[str] = None  # Graph node this action targets
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Status lifecycle
    status: str = "pending"  # pending → applied | superseded
    applied_at: Optional[str] = None

    def to_dynamo(self) -> dict[str, Any]:
        data = self.model_dump()
        data["pk"] = f"INVESTIGATION#{self.investigation_id}"
        data["sk"] = f"CONTROL#{self.timestamp}#{self.action_id}"
        return data


class HypothesisSummary(BaseModel):
    """Condensed hypothesis for state display."""
    name: str
    root_cause: str
    score: float
    is_winner: bool = False
    evidence_refs: list[str] = Field(default_factory=list)


class MemoryMatch(BaseModel):
    """Similar incident from AgentCore memory."""
    incident_id: str
    summary: str
    service: str
    similarity_score: float
    root_cause: Optional[str] = None
    resolution: Optional[str] = None
    occurred_at: Optional[str] = None
    source: str = "ltm"  # stm | ltm | knowledge_graph


class IncidentState(BaseModel):
    """Full investigation state — persisted in DynamoDB."""
    model_config = {"extra": "allow"}

    # Identity
    investigation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    schema_version: str = Field(default=CURRENT_INCIDENT_SCHEMA_VERSION)

    # Tracing
    trace_id: str = Field(default="")
    x_ray_trace_url: Optional[str] = None  # Deeplink to X-Ray console

    # Incident metadata
    summary: str = ""
    affected_service: str = ""
    severity: str = IncidentSeverity.WARNING.value
    incident_type: str = ""    # timeout | oomkill | error_spike | etc.
    source: str = ""           # moogsoft | servicenow | pagerduty

    # Investigation lifecycle
    status: InvestigationStatus = InvestigationStatus.PENDING
    playbook: list[str] = Field(default_factory=list)  # Ordered worker list
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None

    # Results
    root_cause: Optional[str] = None
    confidence: float = 0.0
    risk_level: str = "unknown"   # low | medium | high | critical
    hypotheses: list[HypothesisSummary] = Field(default_factory=list)
    winner_hypothesis: Optional[str] = None

    # Evidence
    receipt_ids: list[str] = Field(default_factory=list)
    tool_calls_total: int = 0
    tool_calls_success: int = 0
    tool_calls_failed: int = 0

    # Memory
    memory_matches: list[MemoryMatch] = Field(default_factory=list)
    judge_scores: dict[str, float] = Field(default_factory=dict)

    # Control
    control_actions: list[ControlAction] = Field(default_factory=list)
    awaiting_approval_for: Optional[str] = None  # Description of pending action

    # Temporal freshness
    data_freshness: dict[str, str] = Field(default_factory=dict)  # worker → last_timestamp
    stale_sources: list[str] = Field(default_factory=list)

    # Replay
    replay_available: bool = False
    replay_artifact_uri: Optional[str] = None  # S3 URI
    event_count: int = 0

    # Budget
    budget_used: int = 0
    budget_max: int = 20

    # TTL for DynamoDB (30 days)
    ttl: int = Field(default_factory=lambda: __import__("time").time().__int__() + 30 * 24 * 3600)

    def to_dynamo(self) -> dict[str, Any]:
        data = self.model_dump()
        data["pk"] = f"INVESTIGATION#{self.investigation_id}"
        data["sk"] = "STATE"
        data["gsi1pk"] = f"INCIDENT#{self.incident_id}"
        data["gsi1sk"] = self.started_at or ""
        data["gsi2pk"] = f"STATUS#{self.status.value}"
        data["gsi2sk"] = self.started_at or ""
        return data

    @property
    def is_stale(self) -> bool:
        return len(self.stale_sources) > 0

    @property
    def budget_pct(self) -> float:
        return (self.budget_used / self.budget_max * 100) if self.budget_max > 0 else 0.0
