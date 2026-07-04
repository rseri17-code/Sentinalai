"""Deterministic investigation planner — the LLM-free planning layer.

Placed under ``supervisor/deterministic_planner/`` rather than
``supervisor/planner/`` because ``supervisor/planner.py`` already
exists as a pre-existing agentic (Think→Act→Observe) planner. This
package is architecturally distinct and does not touch that module.

Public surface:
- :class:`PlannerBuilder` — pure transform PlanContext → InvestigationPlan
- :class:`SkillRegistry` — capability → skill-name mapping (data only,
  no execution)
- ``planner_runtime`` — thin runtime-module adapter that plugs the
  builder into the existing IntelligenceRuntime at POST_PERSIST
"""
from __future__ import annotations

from supervisor.deterministic_planner.planner_builder import (
    PLANNER_VERSION,
    PlannerBuilder,
)
from supervisor.deterministic_planner.planner_registry import (
    DEFAULT_SKILL_REGISTRY,
    SkillRegistry,
)
from supervisor.deterministic_planner.planner_runtime import (
    PLANNER_FEATURE_FLAG,
    PLANNER_SPEC,
    planner_runner,
)


__all__ = [
    "PLANNER_VERSION",
    "PlannerBuilder",
    "SkillRegistry",
    "DEFAULT_SKILL_REGISTRY",
    "PLANNER_SPEC",
    "PLANNER_FEATURE_FLAG",
    "planner_runner",
]
