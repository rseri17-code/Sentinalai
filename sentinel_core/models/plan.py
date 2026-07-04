"""InvestigationPlan + PlanStep — deterministic planner output.

The planner emits an :class:`InvestigationPlan` whose steps are the
capability-level intents needed to satisfy the plan's goals. Nothing in
this file executes anything; a PlanStep only *describes* what to do.

Design principles
-----------------
- **Immutable**: frozen dataclasses; tuple-typed fields.
- **Deterministic**: every id is a sha256[:16] of its logical content
  so identical planner inputs produce byte-identical output.
- **JSON-safe**: ``to_dict()`` sorts nested collections and coerces
  tuples to lists so the output is stable across Python versions.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from sentinel_core.models.goal import InvestigationGoal


PLAN_SCHEMA_VERSION = 1


def make_step_id(goal_id: str, capability_id: str) -> str:
    """Deterministic step id — sha256[:16] of (goal_id, capability_id)."""
    return hashlib.sha256(f"{goal_id}:{capability_id}".encode()).hexdigest()[:16]


def make_plan_id(steps: tuple["PlanStep", ...], target_confidence: int) -> str:
    """Deterministic plan id — sha256[:16] over the sorted step ids and
    the target confidence. Same steps + same target → same id.
    """
    raw = ":".join(sorted(s.step_id for s in steps)) + f"|t{target_confidence}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class PlanStep:
    """One capability-level intent within an :class:`InvestigationPlan`.

    A step is a description of *what* to do, not *how*. Execution is
    the responsibility of a future skill runtime that maps
    ``capability_id`` → concrete skill via the SkillRegistry.
    """
    step_id:                  str
    goal_id:                  str
    capability_id:            str
    reason:                   str
    expected_evidence:        tuple[str, ...] = ()
    success_criteria:         tuple[str, ...] = ()
    failure_criteria:         tuple[str, ...] = ()
    expected_confidence_gain: int = 10
    priority:                 int = 100
    dependencies:             tuple[str, ...] = ()   # prerequisite step ids
    estimated_runtime_ms:     int = 5_000
    schema_version:           int = PLAN_SCHEMA_VERSION

    @classmethod
    def make(
        cls,
        goal_id: str,
        capability_id: str,
        reason: str,
        *,
        expected_evidence: tuple[str, ...] = (),
        success_criteria: tuple[str, ...] = (),
        failure_criteria: tuple[str, ...] = (),
        expected_confidence_gain: int = 10,
        priority: int = 100,
        dependencies: tuple[str, ...] = (),
        estimated_runtime_ms: int = 5_000,
    ) -> "PlanStep":
        return cls(
            step_id=make_step_id(goal_id, capability_id),
            goal_id=goal_id,
            capability_id=capability_id,
            reason=reason,
            expected_evidence=tuple(expected_evidence),
            success_criteria=tuple(success_criteria),
            failure_criteria=tuple(failure_criteria),
            expected_confidence_gain=max(0, min(100, int(expected_confidence_gain))),
            priority=int(priority),
            dependencies=tuple(dependencies),
            estimated_runtime_ms=max(0, int(estimated_runtime_ms)),
        )


@dataclass(frozen=True)
class InvestigationPlan:
    """The deterministic planner's output — an ordered plan.

    Fields:
        plan_id: deterministic sha256[:16] over the plan's steps and
            target confidence.
        goals: the goals this plan pursues, in derivation order.
        steps: the ordered plan steps. Order reflects the planner's
            priority calculation (higher expected_confidence_gain, then
            higher priority, then lexicographic step_id).
        dependency_graph: {step_id: (prerequisite_step_ids, …)} for the
            steps in ``steps``. Only edges within this plan are recorded.
        expected_confidence_progression: cumulative confidence after
            each step (capped at 100). Same length as ``steps``.
        estimated_total_cost: currently step count (an operational proxy).
        estimated_total_latency_ms: sum of steps' estimated_runtime_ms.
    """
    plan_id:                          str
    goals:                            tuple[InvestigationGoal, ...] = ()
    steps:                            tuple[PlanStep, ...] = ()
    dependency_graph:                 dict[str, tuple[str, ...]] = field(default_factory=dict)
    expected_confidence_progression:  tuple[int, ...] = ()
    estimated_total_cost:             int = 0
    estimated_total_latency_ms:       int = 0
    initial_confidence:               int = 0
    target_confidence:                int = 0
    schema_version:                   int = PLAN_SCHEMA_VERSION

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def step_count(self) -> int:
        return len(self.steps)

    def goal_count(self) -> int:
        return len(self.goals)

    def is_empty(self) -> bool:
        return not self.goals and not self.steps

    def final_confidence(self) -> int:
        return int(self.expected_confidence_progression[-1]) \
            if self.expected_confidence_progression else self.initial_confidence

    # ------------------------------------------------------------------
    # Serialization — deterministic, JSON-safe
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Deterministic JSON-safe dict. Nested tuples become lists;
        dependency_graph edges are sorted per key."""
        goals_dicts = [g.to_dict() for g in self.goals]
        steps_dicts = [asdict(s) for s in self.steps]
        dep_out: dict[str, list[str]] = {}
        for k in sorted(self.dependency_graph.keys()):
            dep_out[k] = sorted(self.dependency_graph[k])
        return {
            "plan_id":                         self.plan_id,
            "schema_version":                  self.schema_version,
            "initial_confidence":              self.initial_confidence,
            "target_confidence":               self.target_confidence,
            "final_confidence":                self.final_confidence(),
            "goal_count":                      len(goals_dicts),
            "step_count":                      len(steps_dicts),
            "estimated_total_cost":            self.estimated_total_cost,
            "estimated_total_latency_ms":      self.estimated_total_latency_ms,
            "expected_confidence_progression": list(self.expected_confidence_progression),
            "goals":                           goals_dicts,
            "steps":                           _steps_as_lists(steps_dicts),
            "dependency_graph":                dep_out,
        }


def _steps_as_lists(step_dicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """asdict() preserves tuples on frozen-dataclass fields; normalise to
    lists so the JSON body is stable across Python versions."""
    out = []
    for d in step_dicts:
        cleaned = {}
        for k, v in d.items():
            if isinstance(v, tuple):
                cleaned[k] = list(v)
            else:
                cleaned[k] = v
        out.append(cleaned)
    return out


__all__ = [
    "PLAN_SCHEMA_VERSION",
    "PlanStep",
    "InvestigationPlan",
    "make_step_id",
    "make_plan_id",
]
