"""PlanContext — the deterministic planner's input envelope.

A PlanContext bundles everything the planner needs to derive a plan:
the DecisionContext, the KnowledgeGraph, the phase receipts, and the
completed/outstanding goal state. It never contains anything the
planner can't reach; it never invokes anything itself.

Design principles
-----------------
- **Immutable**: frozen dataclass.
- **Optional fields**: every embedded object is optional; the planner
  degrades gracefully to defaults when data is missing.
- **No mutation**: cloning happens via ``dataclasses.replace`` or the
  ``with_updates`` helper — never in-place.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


PLAN_CONTEXT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PlanContext:
    """Immutable envelope of everything the planner reads.

    Fields:
        service: primary service under investigation.
        incident_type: classified incident type.
        decision_context: optional DecisionContext object (any dataclass
            with matching attribute names is accepted; the planner
            duck-types).
        knowledge_graph: optional KnowledgeGraph object (same duck-type
            tolerance).
        receipts: tuple of already-finalized phase receipts (JSON-safe
            dicts).
        completed_goals: goal ids already achieved. Steps for these are
            skipped.
        outstanding_goals: goal ids the caller wants pursued even if the
            planner would not derive them from state.
        current_confidence: 0-100. The planner stops adding steps once
            projected confidence reaches ``target_confidence``.
        target_confidence: 0-100. Default 80.
        evidence_summary: opaque dict describing what evidence has been
            collected so far; the planner uses it to skip
            already-satisfied capabilities.
    """
    service:            str = ""
    incident_type:      str = ""
    decision_context:   Any = None      # DecisionContext-like
    knowledge_graph:    Any = None      # KnowledgeGraph-like
    receipts:           tuple[dict[str, Any], ...] = ()
    completed_goals:    tuple[str, ...] = ()
    outstanding_goals:  tuple[str, ...] = ()
    current_confidence: int = 50
    target_confidence:  int = 80
    evidence_summary:   dict[str, Any] = field(default_factory=dict)
    schema_version:     int = PLAN_CONTEXT_SCHEMA_VERSION

    def with_updates(self, **kwargs: Any) -> "PlanContext":
        """Return a new PlanContext with the given fields replaced.

        Never mutates ``self``. This is the ONLY sanctioned way for
        callers to derive a modified PlanContext.
        """
        return replace(self, **kwargs)


__all__ = [
    "PLAN_CONTEXT_SCHEMA_VERSION",
    "PlanContext",
]
