"""Capability — WHAT type of investigation satisfies a goal.

Capabilities describe *what* the planner asks for, never *how* it will
be executed. Capability → skill mapping lives inside
``supervisor.deterministic_planner.planner_registry.SkillRegistry``.
Planner never chooses skills; it only chooses capabilities.

Design principles
-----------------
- **Immutable**: frozen dataclass, tuple-typed fields.
- **Deterministic id**: capability_id = ``"cap:" + capability_type``,
  stable across runs.
- **Extensible**: new capability types appended to
  :class:`CapabilityType` never break existing consumers because both
  the enum and dataclass field are ``str``-typed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


CAPABILITY_SCHEMA_VERSION = 1


class CapabilityType(str, Enum):
    """Coarse taxonomy of investigative capabilities.

    Each entry names *what* to collect/verify, not *how*. The concrete
    tool (kubectl, sysdig, prometheus, elastic, otel, …) is decided by
    the SkillRegistry at execution time — a future milestone.
    """
    COLLECT_POD_LIFECYCLE        = "collect_pod_lifecycle"
    COLLECT_DEPLOYMENT_HISTORY   = "collect_deployment_history"
    COLLECT_DNS_STATE            = "collect_dns_state"
    COLLECT_LATENCY              = "collect_latency"
    COLLECT_TOPOLOGY             = "collect_topology"
    COLLECT_HISTORICAL_INCIDENTS = "collect_historical_incidents"
    COLLECT_TRANSACTION_PATH     = "collect_transaction_path"
    COLLECT_LOGS                 = "collect_logs"
    COLLECT_METRICS              = "collect_metrics"
    COLLECT_STORAGE_METRICS      = "collect_storage_metrics"
    COLLECT_AUTH_EVENTS          = "collect_auth_events"
    COMPARE_HISTORICAL_FAILURES  = "compare_historical_failures"
    QUERY_KNOWLEDGE_GRAPH        = "query_knowledge_graph"
    ASSESS_DEPENDENCY_HEALTH     = "assess_dependency_health"
    ASSESS_BLAST_RADIUS          = "assess_blast_radius"


def make_capability_id(capability_type: str) -> str:
    """Capability id = ``"cap:" + capability_type``. Stable across runs."""
    return "cap:" + str(capability_type)


@dataclass(frozen=True)
class Capability:
    """A named investigative capability with metadata for the planner.

    Fields:
        capability_id: deterministic id derived from capability_type.
        capability_type: value of :class:`CapabilityType`.
        description: human-readable summary of what this capability does.
        satisfies_goal_types: goal types this capability can contribute to.
        typical_evidence_yield: evidence keys this capability typically
            produces (used by :class:`PlanStep.expected_evidence`).
        typical_confidence_gain: 0-100, average confidence delta the
            planner assumes when scheduling this capability.
        typical_runtime_ms: rough runtime estimate; used for latency
            budget in :class:`InvestigationPlan`.
    """
    capability_id:           str
    capability_type:         str
    description:             str
    satisfies_goal_types:    tuple[str, ...] = ()
    typical_evidence_yield:  tuple[str, ...] = ()
    typical_confidence_gain: int = 10
    typical_runtime_ms:      int = 5_000
    schema_version:          int = CAPABILITY_SCHEMA_VERSION

    @classmethod
    def make(
        cls,
        capability_type: CapabilityType | str,
        description: str,
        *,
        satisfies_goal_types: tuple[str, ...] = (),
        typical_evidence_yield: tuple[str, ...] = (),
        typical_confidence_gain: int = 10,
        typical_runtime_ms: int = 5_000,
    ) -> "Capability":
        ct = capability_type.value if isinstance(capability_type, CapabilityType) else str(capability_type)
        return cls(
            capability_id=make_capability_id(ct),
            capability_type=ct,
            description=description,
            satisfies_goal_types=tuple(satisfies_goal_types),
            typical_evidence_yield=tuple(typical_evidence_yield),
            typical_confidence_gain=max(0, min(100, int(typical_confidence_gain))),
            typical_runtime_ms=max(0, int(typical_runtime_ms)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "CapabilityType",
    "Capability",
    "make_capability_id",
]
