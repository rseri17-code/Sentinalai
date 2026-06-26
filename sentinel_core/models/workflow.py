"""Workflow execution contracts for SentinalAI durable investigations.

Zero-dependency types that model the lifecycle of a durable investigation.
No business logic — contracts only.

Dependency rule: imports only stdlib + typing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class WorkflowPhase(str, Enum):
    """Named phases that map to agent.py's logical investigation stages."""
    FETCH    = "fetch"      # fetch incident data
    CLASSIFY = "classify"   # incident type classification
    COLLECT  = "collect"    # playbook evidence gathering
    ANALYZE  = "analyze"    # hypothesis generation + RCA
    PERSIST  = "persist"    # store results


class WorkflowStatus(str, Enum):
    PENDING   = "pending"    # created, not yet started
    RUNNING   = "running"    # currently executing
    COMPLETED = "completed"  # finished successfully
    FAILED    = "failed"     # finished with error
    RESUMED   = "resumed"    # was orphaned, then restarted


class PhaseStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"


@dataclass
class ExecutionMetadata:
    """Lightweight metadata recorded with each workflow event."""
    incident_id: str = ""
    incident_type: str = ""
    service: str = ""
    severity: int = 3
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseResult:
    """Record of a single phase execution."""
    phase: str
    status: PhaseStatus
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at) * 1000
        return 0.0


@dataclass
class WorkflowCheckpoint:
    """Snapshot of investigation state at a point in execution.

    Persisted to SQLite so a crashed investigation can be detected and
    optionally resumed from the last successful checkpoint.
    """
    investigation_id: str
    phase: str                              # last completed phase
    status: WorkflowStatus
    completed_phases: list[str] = field(default_factory=list)
    evidence_snapshot: dict[str, Any] = field(default_factory=dict)
    result_snapshot: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowState:
    """Full execution state for one investigation workflow."""
    investigation_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_phase: str = ""
    completed_phases: list[str] = field(default_factory=list)
    phase_results: list[PhaseResult] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: Optional[float] = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED)

    @property
    def is_orphaned(self) -> bool:
        """True when running but no completion was ever recorded."""
        return self.status == WorkflowStatus.RUNNING


@runtime_checkable
class WorkflowPort(Protocol):
    """Protocol for durable workflow backends."""

    def start(self, investigation_id: str, metadata: dict[str, Any]) -> bool: ...
    def checkpoint(
        self,
        investigation_id: str,
        phase: str,
        evidence_snapshot: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None: ...
    def resume(self, investigation_id: str) -> Optional[WorkflowCheckpoint]: ...
    def complete(self, investigation_id: str, result_summary: dict[str, Any]) -> None: ...
    def fail(self, investigation_id: str, error: str) -> None: ...
