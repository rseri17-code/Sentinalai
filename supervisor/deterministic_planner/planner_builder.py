"""PlannerBuilder — pure deterministic transform PlanContext →
InvestigationPlan.

The builder is stateless. It never mutates its inputs, never invokes
the LLM, never calls into the runtime, never touches any store. Same
PlanContext → byte-identical InvestigationPlan.

Public surface:
- :class:`PlannerBuilder`
- :data:`PLANNER_VERSION`

Design principles
-----------------
- **Pure**: no I/O, no timestamps, no randomness.
- **Deterministic**: goals sorted by (-priority, goal_id); steps sorted
  by (-expected_confidence_gain, -priority, step_id); dependency_graph
  and expected_confidence_progression are closed-form projections.
- **Bounded**: number of steps ≤ ``max_steps`` (default 32). Once
  cumulative confidence reaches ``target_confidence`` no further steps
  are appended.
- **Non-mutating**: PlanContext, DecisionContext, KnowledgeGraph are
  never mutated; the builder only reads via getattr.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sentinel_core.models.capability import Capability
from sentinel_core.models.goal import InvestigationGoal
from sentinel_core.models.plan import (
    InvestigationPlan,
    PlanStep,
    make_plan_id,
)
from sentinel_core.models.plan_context import PlanContext
from supervisor.deterministic_planner.planner_registry import SkillRegistry
from supervisor.deterministic_planner.planner_rules import (
    compute_dependencies,
    derive_goals,
    select_capabilities_for_goal,
)


PLANNER_VERSION = 1

# Hard upper bound on plan size. Deterministic guardrail so a corpus
# with many outstanding goals never produces an unbounded plan.
_DEFAULT_MAX_STEPS = 32


class PlannerBuilder:
    """Stateless builder: PlanContext → InvestigationPlan.

    The builder holds an optional SkillRegistry for future observability
    (the current step output does not include skill names — the planner
    outputs capability-level intents only, per the mission's permanent
    contract).
    """

    def __init__(self, skill_registry: Optional[SkillRegistry] = None,
                 max_steps: int = _DEFAULT_MAX_STEPS) -> None:
        self._registry  = skill_registry or SkillRegistry()
        self._max_steps = int(max_steps)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    def build(self, plan_context: PlanContext) -> InvestigationPlan:
        """Build the plan. Never raises on missing data; degrades to
        an empty plan with default fields when everything is empty."""
        if plan_context is None:
            return InvestigationPlan(
                plan_id=make_plan_id((), 0),
                goals=(),
                steps=(),
                dependency_graph={},
                expected_confidence_progression=(),
                initial_confidence=0,
                target_confidence=0,
            )

        current_confidence = _clamp_int(
            getattr(plan_context, "current_confidence", 50), 0, 100,
        )
        target_confidence = _clamp_int(
            getattr(plan_context, "target_confidence", 80), 0, 100,
        )

        goals = derive_goals(plan_context)

        # Build candidate steps: one per (goal, capability) pair, but the
        # same capability is only ever scheduled once.
        used_capabilities: set[str] = set()
        candidate_steps: list[PlanStep] = []
        for goal in goals:
            for cap in select_capabilities_for_goal(goal.goal_type):
                if cap.capability_id in used_capabilities:
                    continue
                used_capabilities.add(cap.capability_id)
                step = self._make_step(goal, cap, plan_context)
                candidate_steps.append(step)

        # Deterministic ordering: highest expected_confidence_gain first;
        # then higher priority; then lexicographic step_id for stable ties.
        candidate_steps.sort(
            key=lambda s: (-s.expected_confidence_gain, -s.priority, s.step_id)
        )

        # Cap by target_confidence + max_steps. Cumulate confidence
        # progression as we go.
        selected_steps: list[PlanStep] = []
        progression: list[int] = []
        cumulative = current_confidence
        for step in candidate_steps:
            if len(selected_steps) >= self._max_steps:
                break
            if cumulative >= target_confidence:
                break
            selected_steps.append(step)
            cumulative = min(100, cumulative + step.expected_confidence_gain)
            progression.append(cumulative)

        deps = compute_dependencies(tuple(selected_steps))
        total_cost = len(selected_steps)
        total_latency = sum(s.estimated_runtime_ms for s in selected_steps)
        plan_id = make_plan_id(tuple(selected_steps), target_confidence)

        return InvestigationPlan(
            plan_id=plan_id,
            goals=goals,
            steps=tuple(selected_steps),
            dependency_graph=deps,
            expected_confidence_progression=tuple(progression),
            estimated_total_cost=total_cost,
            estimated_total_latency_ms=total_latency,
            initial_confidence=current_confidence,
            target_confidence=target_confidence,
        )

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    def registry(self) -> SkillRegistry:
        """Return the associated SkillRegistry (read-only)."""
        return self._registry

    def max_steps(self) -> int:
        return self._max_steps

    # ------------------------------------------------------------------
    # Step construction
    # ------------------------------------------------------------------

    def _make_step(
        self,
        goal: InvestigationGoal,
        capability: Capability,
        plan_context: PlanContext,
    ) -> PlanStep:
        # Priority is a deterministic function of goal priority and the
        # capability's typical confidence gain.
        step_priority = goal.priority + capability.typical_confidence_gain

        # Success/failure criteria borrow from the goal's own criteria
        # so the caller sees a coherent chain.
        return PlanStep.make(
            goal_id=goal.goal_id,
            capability_id=capability.capability_id,
            reason=f"Satisfies goal '{goal.description}' via {capability.description}",
            expected_evidence=capability.typical_evidence_yield,
            success_criteria=goal.completion_criteria,
            failure_criteria=goal.failure_criteria,
            expected_confidence_gain=capability.typical_confidence_gain,
            priority=step_priority,
            dependencies=(),                       # populated post-hoc
            estimated_runtime_ms=capability.typical_runtime_ms,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp_int(value, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


__all__ = [
    "PLANNER_VERSION",
    "PlannerBuilder",
]
