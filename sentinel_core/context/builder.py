"""ContextBuilder — optional factory for InvestigationContext.

Callers are NOT required to use this. Existing per-investigation construction
(``ReceiptCollector(case_id=incident_id)``, ``ExecutionBudget(case_id=...)``,
plain dict evidence, etc.) continues to work. This module is opt-in.

Typical use:

    # Tests / new wiring:
    ctx = ContextBuilder.for_incident("INC12345", incident={"service": "checkout"})
    ctx = ctx.with_classified(incident_type="error_spike", service="checkout", severity=2)

    # From a workflow checkpoint:
    snap = ContextSnapshot.from_dict(checkpoint_dict)
    ctx  = ContextBuilder.from_snapshot(snap)
"""
from __future__ import annotations

from typing import Any, Optional

from sentinel_core.context.investigation import (
    ContextSnapshot,
    InvestigationContext,
)


class ContextBuilder:
    """Static factory methods. No state, no instances."""

    @staticmethod
    def for_incident(
        incident_id: str,
        *,
        incident: Optional[dict[str, Any]] = None,
        receipts: Any = None,
        budget: Any = None,
        circuits: Any = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> InvestigationContext:
        """Build a fresh context for a new investigation.

        ``investigation_id`` is auto-derived as ``f"inv-{incident_id}"``.
        ``created_at`` is auto-stamped from ``time.time()``.
        """
        if not incident_id:
            raise ValueError("incident_id is required")
        return InvestigationContext(
            incident_id=incident_id,
            incident=dict(incident or {}),
            receipts=receipts,
            budget=budget,
            circuits=circuits,
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def from_snapshot(snap: ContextSnapshot) -> InvestigationContext:
        """Rehydrate a context from a stored ContextSnapshot.

        Handles (receipts, budget, circuits) are NOT recovered — the caller
        must re-attach them via ``ctx.with_handles(...)``. The incident
        payload is also not recovered (snapshots intentionally omit it).
        """
        return InvestigationContext(
            incident_id      = snap.incident_id,
            investigation_id = snap.investigation_id,
            incident_type    = snap.incident_type,
            service          = snap.service,
            severity         = snap.severity,
            current_phase    = snap.current_phase,
            created_at       = snap.created_at,
            metadata         = dict(snap.metadata),
        )


__all__ = ["ContextBuilder"]
