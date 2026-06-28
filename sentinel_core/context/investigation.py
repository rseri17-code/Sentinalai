"""InvestigationContext — shared per-investigation state object.

Carries the identifiers and per-investigation handles that today are threaded
through agent.py method signatures as 7+ positional arguments. Introduced as
an additive, opt-in object; existing call sites continue to work unchanged.

Design rules (from Phase 6 spec):
- Frozen where practical (identifiers cannot be reassigned in-place)
- Explicit ``with_*`` mutation methods return a new instance
- Typed and serializable via ``ContextSnapshot``
- Replay-friendly: snapshot is JSON-roundtrippable
- Workflow-compatible: ``to_workflow_metadata()`` produces a dict suitable
  for ``WorkflowEngine.start(metadata=...)``

Mutable handles (receipts, budget, circuits) are accepted as ``Any`` so this
module stays inside ``sentinel_core`` and free of any supervisor/worker deps.
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# Sentinel for "argument not supplied" — lets ``with_*`` methods distinguish
# "leave unchanged" from "set to None".
class _Unset:
    _instance: Optional["_Unset"] = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET: Any = _Unset()


# ---------------------------------------------------------------------------
# ContextSnapshot — JSON-roundtrippable subset
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextSnapshot:
    """JSON-safe view of an InvestigationContext.

    Holds only identifiers and lightweight metadata. Does **not** carry
    ``receipts``, ``budget``, ``circuits``, or any object with locks /
    threads / non-trivial serialization. Suitable for workflow checkpointing,
    replay manifests, and audit logs.
    """
    incident_id: str
    investigation_id: str
    incident_type: str = ""
    service: str = ""
    severity: int = 3
    current_phase: str = ""
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id":      self.incident_id,
            "investigation_id": self.investigation_id,
            "incident_type":    self.incident_type,
            "service":          self.service,
            "severity":         self.severity,
            "current_phase":    self.current_phase,
            "created_at":       self.created_at,
            "metadata":         dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContextSnapshot":
        return cls(
            incident_id      = str(d.get("incident_id", "")),
            investigation_id = str(d.get("investigation_id", "")),
            incident_type    = str(d.get("incident_type", "")),
            service          = str(d.get("service", "")),
            severity         = int(d.get("severity", 3)),
            current_phase    = str(d.get("current_phase", "")),
            created_at       = float(d.get("created_at", 0.0)),
            metadata         = dict(d.get("metadata", {})),
        )


# ---------------------------------------------------------------------------
# InvestigationContext — the carrier
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InvestigationContext:
    """Shared per-investigation state object.

    Frozen at the field level — fields cannot be reassigned in place. Use
    ``with_phase`` / ``with_handles`` / ``with_classified`` to derive an
    updated instance. The mutable handles (``receipts``, ``budget``,
    ``circuits``) themselves accumulate state internally; that's by design.
    """

    # Required identifier
    incident_id: str

    # Derived / classification identifiers (defaults make tests cheap to write)
    investigation_id: str = ""
    incident_type: str = ""
    service: str = ""
    severity: int = 3

    # Already-fetched incident payload (kept small; do NOT pack large blobs here)
    incident: dict[str, Any] = field(default_factory=dict)

    # Per-investigation handles. Typed Any so sentinel_core stays dep-free.
    # In practice these are: ReceiptCollector, ExecutionBudget, CircuitBreakerRegistry.
    receipts: Any = None
    budget: Any = None
    circuits: Any = None

    # Phase tracking — single string, matches sentinel_core.models.workflow.WorkflowPhase
    current_phase: str = ""

    # Lifecycle / introspection
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Auto-derive investigation_id and created_at if not provided.
        if not self.investigation_id:
            object.__setattr__(self, "investigation_id", f"inv-{self.incident_id}")
        if self.created_at == 0.0:
            object.__setattr__(self, "created_at", time.time())

    # ------------------------------------------------------------------
    # Copy / update
    # ------------------------------------------------------------------

    def with_phase(self, phase: str) -> "InvestigationContext":
        """Return a copy with ``current_phase`` updated."""
        return dataclasses.replace(self, current_phase=phase)

    def with_classified(
        self,
        *,
        incident_type: Any = _UNSET,
        service: Any = _UNSET,
        severity: Any = _UNSET,
    ) -> "InvestigationContext":
        """Return a copy with classification fields updated."""
        return dataclasses.replace(
            self,
            incident_type=self.incident_type if incident_type is _UNSET else str(incident_type),
            service=self.service if service is _UNSET else str(service),
            severity=self.severity if severity is _UNSET else int(severity),
        )

    def with_handles(
        self,
        *,
        receipts: Any = _UNSET,
        budget: Any = _UNSET,
        circuits: Any = _UNSET,
    ) -> "InvestigationContext":
        """Return a copy with mutable handles attached / replaced.

        ``None`` is a valid value (clears the handle). To leave a handle
        unchanged, simply omit it.
        """
        return dataclasses.replace(
            self,
            receipts=self.receipts if receipts is _UNSET else receipts,
            budget=self.budget   if budget   is _UNSET else budget,
            circuits=self.circuits if circuits is _UNSET else circuits,
        )

    def with_incident(self, incident: dict[str, Any]) -> "InvestigationContext":
        """Return a copy with the fetched incident payload attached."""
        return dataclasses.replace(self, incident=dict(incident))

    def with_metadata(self, **kv: Any) -> "InvestigationContext":
        """Return a copy with extra metadata merged in."""
        merged = dict(self.metadata)
        merged.update(kv)
        return dataclasses.replace(self, metadata=merged)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def snapshot(self) -> ContextSnapshot:
        """Return a JSON-safe snapshot (drops handles and incident payload)."""
        return ContextSnapshot(
            incident_id      = self.incident_id,
            investigation_id = self.investigation_id,
            incident_type    = self.incident_type,
            service          = self.service,
            severity         = self.severity,
            current_phase    = self.current_phase,
            created_at       = self.created_at,
            metadata         = dict(self.metadata),
        )

    def to_workflow_metadata(self) -> dict[str, Any]:
        """Format compatible with ``WorkflowEngine.start(metadata=...)``."""
        return {
            "incident_id":   self.incident_id,
            "incident_type": self.incident_type,
            "service":       self.service,
            "severity":      self.severity,
            **self.metadata,
        }
