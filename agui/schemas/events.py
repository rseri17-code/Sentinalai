"""AG UI Event Schema v1.0

Every event emitted by the agent or BFF must conform to this schema.
Events are the atomic units of execution visibility.

Versioning strategy:
  - schema_version is semver (MAJOR.MINOR)
  - MAJOR bump = breaking change (field removed or type changed)
  - MINOR bump = additive change (new optional field)
  - All readers must handle unknown fields (extra='allow')
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


CURRENT_SCHEMA_VERSION = "1.0"


class EventType(str, Enum):
    # Investigation lifecycle
    INVESTIGATION_STARTED = "investigation.started"
    INVESTIGATION_COMPLETED = "investigation.completed"
    INVESTIGATION_FAILED = "investigation.failed"
    INVESTIGATION_PAUSED = "investigation.paused"
    INVESTIGATION_RESUMED = "investigation.resumed"

    # Tool execution
    TOOL_CALLED = "tool.called"
    TOOL_RESPONDED = "tool.responded"
    TOOL_FAILED = "tool.failed"
    TOOL_TIMEOUT = "tool.timeout"

    # Hypothesis engine
    HYPOTHESIS_GENERATED = "hypothesis.generated"
    HYPOTHESIS_SCORED = "hypothesis.scored"
    HYPOTHESIS_SELECTED = "hypothesis.selected"

    # LLM operations
    LLM_INVOKED = "llm.invoked"
    LLM_RESPONDED = "llm.responded"
    LLM_FAILED = "llm.failed"

    # Memory
    MEMORY_QUERIED = "memory.queried"
    MEMORY_RESULT = "memory.result"
    MEMORY_STORED = "memory.stored"

    # Human-in-the-loop
    CONTROL_REQUESTED = "control.requested"
    CONTROL_APPROVED = "control.approved"
    CONTROL_REJECTED = "control.rejected"
    CONTROL_TIMEOUT = "control.timeout"

    # Guardrails
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXHAUSTED = "budget.exhausted"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker.tripped"
    CIRCUIT_BREAKER_RESET = "circuit_breaker.reset"

    # Classification
    INCIDENT_CLASSIFIED = "incident.classified"
    PLAYBOOK_SELECTED = "playbook.selected"

    # RCA
    RCA_GENERATED = "rca.generated"

    # Replay
    REPLAY_STARTED = "replay.started"
    REPLAY_STEP = "replay.step"
    REPLAY_COMPLETED = "replay.completed"

    # Postmortem collaboration
    POSTMORTEM_COMMENT_ADDED = "postmortem.comment_added"

    # Dev loop — closed-loop agent-driven development
    DEV_TASK_CREATED         = "dev.task_created"
    DEV_IMPLEMENTING         = "dev.implementing"
    DEV_VALIDATION_STARTED   = "dev.validation_started"
    DEV_VALIDATION_PASSED    = "dev.validation_passed"
    DEV_VALIDATION_FAILED    = "dev.validation_failed"
    DEV_VALIDATION_ITERATING = "dev.validation_iterating"
    DEV_PR_CREATED           = "dev.pr_created"
    DEV_CI_STARTED           = "dev.ci_started"
    DEV_CI_PASSED            = "dev.ci_passed"
    DEV_CI_FAILED            = "dev.ci_failed"
    DEV_CI_FIXING            = "dev.ci_fixing"
    DEV_REVIEW_COMMENT       = "dev.review_comment"
    DEV_REVIEW_RESPONDED     = "dev.review_responded"
    DEV_COMPLETED            = "dev.completed"
    DEV_NEEDS_HUMAN          = "dev.needs_human"
    DEV_FAILED               = "dev.failed"

    # Pattern Intelligence Layer
    INTELLIGENCE_PREDICTION  = "intelligence.prediction"
    INTELLIGENCE_SLO_BURNING = "intelligence.slo_burning"
    INTELLIGENCE_CYCLE_DONE  = "intelligence.cycle_done"

    # System
    HEARTBEAT = "system.heartbeat"


class AGUIEvent(BaseModel):
    """Immutable event record. Once emitted, never mutated.

    idempotency_key = SHA256(investigation_id + ":" + str(sequence_num))
    ensures exactly-once processing.
    """
    model_config = {"extra": "allow"}

    # Identity
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = Field(default=CURRENT_SCHEMA_VERSION)
    idempotency_key: str = Field(default="")

    # Classification
    event_type: EventType
    investigation_id: str
    incident_id: str

    # Tracing (aligned with OTEL / X-Ray)
    trace_id: str = Field(default="")
    span_id: str = Field(default="")
    parent_span_id: Optional[str] = None

    # Ordering
    sequence_num: int = Field(default=0)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    timestamp_epoch_ms: int = Field(
        default_factory=lambda: int(time.time() * 1000)
    )

    # Payload (event-type-specific, always structured)
    payload: dict[str, Any] = Field(default_factory=dict)

    # TTL for DynamoDB (Unix timestamp, 7 days from creation)
    ttl: int = Field(
        default_factory=lambda: int(time.time()) + 7 * 24 * 3600
    )

    @model_validator(mode="after")
    def set_idempotency_key(self) -> "AGUIEvent":
        if not self.idempotency_key:
            raw = f"{self.investigation_id}:{self.sequence_num}"
            self.idempotency_key = hashlib.sha256(raw.encode()).hexdigest()[:32]
        return self

    def to_dynamo(self) -> dict[str, Any]:
        """Serialize for DynamoDB PutItem."""
        data = self.model_dump()
        data["pk"] = f"INVESTIGATION#{self.investigation_id}"
        data["sk"] = f"EVENT#{self.sequence_num:08d}#{self.event_id}"
        return data

    def to_ws_message(self) -> str:
        """Serialize for WebSocket broadcast."""
        return json.dumps(self.model_dump(), default=str)

    @classmethod
    def make_tool_called(
        cls,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        span_id: str,
        sequence_num: int,
        worker: str,
        action: str,
        params: dict[str, Any],
        receipt_id: str,
        parent_span_id: Optional[str] = None,
    ) -> "AGUIEvent":
        return cls(
            event_type=EventType.TOOL_CALLED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            sequence_num=sequence_num,
            payload={
                "worker": worker,
                "action": action,
                "params": params,
                "receipt_id": receipt_id,
            },
        )

    @classmethod
    def make_tool_responded(
        cls,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        span_id: str,
        sequence_num: int,
        worker: str,
        action: str,
        receipt_id: str,
        elapsed_ms: float,
        result_count: int,
        status: str,
        error: Optional[str] = None,
    ) -> "AGUIEvent":
        return cls(
            event_type=EventType.TOOL_RESPONDED if status == "success" else EventType.TOOL_FAILED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            span_id=span_id,
            sequence_num=sequence_num,
            payload={
                "worker": worker,
                "action": action,
                "receipt_id": receipt_id,
                "elapsed_ms": elapsed_ms,
                "result_count": result_count,
                "status": status,
                "error": error,
            },
        )

    @classmethod
    def make_hypothesis_scored(
        cls,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        sequence_num: int,
        hypotheses: list[dict[str, Any]],
        winner: str,
        confidence: float,
    ) -> "AGUIEvent":
        return cls(
            event_type=EventType.HYPOTHESIS_SCORED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=sequence_num,
            payload={
                "hypotheses": hypotheses,
                "winner": winner,
                "confidence": confidence,
            },
        )


class EventSchema(BaseModel):
    """Schema registry entry."""
    event_type: EventType
    schema_version: str
    required_payload_fields: list[str]
    description: str
