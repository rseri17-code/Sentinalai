"""InvestigationGoal — WHAT the deterministic planner must prove.

Goals describe *outcomes* the planner must achieve for the current
investigation. They are entirely LLM-free; every goal is a closed-form
projection of ``DecisionContext`` + ``KnowledgeGraph`` state derived
via the rules in ``supervisor.deterministic_planner.planner_rules``.

Design principles
-----------------
- **Immutable**: frozen dataclass, tuple-typed fields.
- **Deterministic**: goal_id derived from goal_type + description via
  sha256[:16] so the same logical goal has the same id across runs.
- **Bounded**: no unbounded lists; every goal is a compact record.
- **Extensible**: new goal types appended to ``GoalType`` never break
  existing consumers because both the enum and dataclass field are
  ``str``-typed.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


GOAL_SCHEMA_VERSION = 1


class GoalType(str, Enum):
    """Coarse taxonomy of investigation goals."""
    VALIDATE_DEPLOYMENT_HYPOTHESIS   = "validate_deployment_hypothesis"
    DETERMINE_NETWORK_FAILURE        = "determine_network_failure"
    VALIDATE_KUBERNETES_HEALTH       = "validate_kubernetes_health"
    DETERMINE_AUTHENTICATION_FAILURE = "determine_authentication_failure"
    DETERMINE_STORAGE_BOTTLENECK     = "determine_storage_bottleneck"
    COMPARE_HISTORICAL_FAILURES      = "compare_historical_failures"
    ASSESS_BLAST_RADIUS              = "assess_blast_radius"
    VALIDATE_DEPENDENCY_HEALTH       = "validate_dependency_health"
    COLLECT_ROOT_CAUSE_EVIDENCE      = "collect_root_cause_evidence"


def make_goal_id(goal_type: str, description: str) -> str:
    """Deterministic goal id — sha256[:16] of (type, description)."""
    raw = f"{goal_type}:{description}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class InvestigationGoal:
    """An outcome the planner must prove.

    Fields:
        goal_id: deterministic id (see :func:`make_goal_id`).
        goal_type: value of :class:`GoalType` (str for forward-compat).
        description: human-readable one-line description; used for id.
        priority: 1-1000, higher = more important. Used to break ties in
            step ordering.
        completion_criteria: strings that describe what satisfies the goal.
        failure_criteria: strings that describe conditions under which the
            goal has failed (planner should stop pursuing).
        expected_confidence_gain: 0-100, contribution to overall
            investigation confidence if the goal is achieved.
    """
    goal_id:                  str
    goal_type:                str
    description:              str
    priority:                 int = 100
    completion_criteria:      tuple[str, ...] = ()
    failure_criteria:         tuple[str, ...] = ()
    expected_confidence_gain: int = 10
    schema_version:           int = GOAL_SCHEMA_VERSION

    @classmethod
    def make(
        cls,
        goal_type: GoalType | str,
        description: str,
        *,
        priority: int = 100,
        completion_criteria: tuple[str, ...] = (),
        failure_criteria: tuple[str, ...] = (),
        expected_confidence_gain: int = 10,
    ) -> "InvestigationGoal":
        gt = goal_type.value if isinstance(goal_type, GoalType) else str(goal_type)
        return cls(
            goal_id=make_goal_id(gt, description),
            goal_type=gt,
            description=description,
            priority=int(priority),
            completion_criteria=tuple(completion_criteria),
            failure_criteria=tuple(failure_criteria),
            expected_confidence_gain=max(0, min(100, int(expected_confidence_gain))),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "GOAL_SCHEMA_VERSION",
    "GoalType",
    "InvestigationGoal",
    "make_goal_id",
]
