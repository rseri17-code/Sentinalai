"""PhaseExecutionReceipt — per-phase execution audit record.

Behavior-neutral: receipts are populated by a lightweight collector inside
investigate() and attached to the returned result under the internal
``_phase_receipts`` key (matching the existing underscore-prefixed metadata
convention used by ``_evidence_snapshot``, ``_gate_post_collection``,
``_llm_metrics``, ``_grounding``, etc.).

The receipt fields are chosen so no phase output ever needs semantic
interpretation:

- ``phase_name``: taken verbatim from ``PhaseResult.phase``
- ``status``: mapped from ``PhaseResult.status`` enum via a static table
- ``started_at`` / ``completed_at`` / ``elapsed_ms``: monotonic clock,
  captured by the collector's context manager
- ``evidence_count_before`` / ``_after``: only populated where the caller
  passes a value (Collect and Analyze have safe access; other phases
  default to 0)
- ``error_type``: populated only when the phase's context manager catches
  an exception — the exception is re-raised, never masked
- ``metadata``: freeform key/value bag for future extension; JSON-safe types
  only

All string fields default to ``""`` and integer fields to ``0``, so JSON
round-trip is safe even with the minimum-viable payload.

Dependency rule: stdlib + typing only (belongs alongside the other
zero-dependency models in ``sentinel_core.models``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_SUCCESS  = "success"
STATUS_DEGRADED = "degraded"
STATUS_SKIPPED  = "skipped"
STATUS_FAILED   = "failed"

_ALLOWED_STATUSES = frozenset({
    STATUS_SUCCESS, STATUS_DEGRADED, STATUS_SKIPPED, STATUS_FAILED,
})


def normalize_status(value: str) -> str:
    """Return a canonical status string, defaulting to ``success`` on unknown."""
    v = (value or "").strip().lower()
    return v if v in _ALLOWED_STATUSES else STATUS_SUCCESS


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhaseExecutionReceipt:
    """Immutable record of one phase's execution.

    Fields are ordered from most-important (phase_name, status) to
    most-optional (metadata). All fields have safe defaults so partial
    receipts still round-trip cleanly through JSON.
    """
    phase_name: str
    status: str = STATUS_SUCCESS
    started_at: float = 0.0
    completed_at: float = 0.0
    elapsed_ms: float = 0.0
    evidence_count_before: int = 0
    evidence_count_after: int = 0
    warnings: tuple[str, ...] = ()
    degraded_reason: str = ""
    error_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict that json.dumps can serialize."""
        return {
            "phase_name":            self.phase_name,
            "status":                self.status,
            "started_at":            self.started_at,
            "completed_at":          self.completed_at,
            "elapsed_ms":            self.elapsed_ms,
            "evidence_count_before": self.evidence_count_before,
            "evidence_count_after":  self.evidence_count_after,
            "warnings":              list(self.warnings),
            "degraded_reason":       self.degraded_reason,
            "error_type":            self.error_type,
            "metadata":              dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PhaseExecutionReceipt":
        """Rehydrate from a to_dict()-shaped dict. Tolerates missing fields."""
        d = d or {}
        warnings_v = d.get("warnings") or ()
        if not isinstance(warnings_v, (list, tuple)):
            warnings_v = ()
        metadata_v = d.get("metadata") or {}
        if not isinstance(metadata_v, dict):
            metadata_v = {}
        return cls(
            phase_name            = str(d.get("phase_name", "")),
            status                = normalize_status(str(d.get("status", ""))),
            started_at            = float(d.get("started_at",   0.0) or 0.0),
            completed_at          = float(d.get("completed_at", 0.0) or 0.0),
            elapsed_ms            = float(d.get("elapsed_ms",   0.0) or 0.0),
            evidence_count_before = int(d.get("evidence_count_before", 0) or 0),
            evidence_count_after  = int(d.get("evidence_count_after",  0) or 0),
            warnings              = tuple(str(w) for w in warnings_v),
            degraded_reason       = str(d.get("degraded_reason", "")),
            error_type            = str(d.get("error_type",     "")),
            metadata              = dict(metadata_v),
        )


__all__ = [
    "PhaseExecutionReceipt",
    "STATUS_SUCCESS", "STATUS_DEGRADED", "STATUS_SKIPPED", "STATUS_FAILED",
    "normalize_status",
]
