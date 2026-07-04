"""Planner model facade — re-exports the canonical planner models.

Provided so downstream consumers can write::

    from sentinel_core.models.planner import (
        InvestigationGoal, Capability, PlanStep, InvestigationPlan,
        PlanContext,
    )

instead of importing from four separate modules. This file contains
NO logic; it is a re-export point only.
"""
from __future__ import annotations

from sentinel_core.models.capability import (
    CAPABILITY_SCHEMA_VERSION,
    Capability,
    CapabilityType,
    make_capability_id,
)
from sentinel_core.models.goal import (
    GOAL_SCHEMA_VERSION,
    GoalType,
    InvestigationGoal,
    make_goal_id,
)
from sentinel_core.models.plan import (
    PLAN_SCHEMA_VERSION,
    InvestigationPlan,
    PlanStep,
    make_plan_id,
    make_step_id,
)
from sentinel_core.models.plan_context import (
    PLAN_CONTEXT_SCHEMA_VERSION,
    PlanContext,
)


__all__ = [
    # Enums
    "GoalType",
    "CapabilityType",
    # Models
    "InvestigationGoal",
    "Capability",
    "PlanStep",
    "InvestigationPlan",
    "PlanContext",
    # Deterministic id helpers
    "make_goal_id",
    "make_capability_id",
    "make_step_id",
    "make_plan_id",
    # Schema versions
    "GOAL_SCHEMA_VERSION",
    "CAPABILITY_SCHEMA_VERSION",
    "PLAN_SCHEMA_VERSION",
    "PLAN_CONTEXT_SCHEMA_VERSION",
]
