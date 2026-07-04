"""Pure-library tests for the planner canonical models."""
from __future__ import annotations

import json
import pytest

from sentinel_core.models.planner import (
    CAPABILITY_SCHEMA_VERSION,
    Capability,
    CapabilityType,
    GOAL_SCHEMA_VERSION,
    GoalType,
    InvestigationGoal,
    InvestigationPlan,
    PLAN_SCHEMA_VERSION,
    PlanContext,
    PlanStep,
    make_capability_id,
    make_goal_id,
    make_plan_id,
    make_step_id,
)


# ---------------------------------------------------------------------------
# Deterministic ids
# ---------------------------------------------------------------------------

class TestIds:
    def test_goal_id_deterministic(self):
        a = make_goal_id("validate_deployment_hypothesis", "Validate deploy X")
        b = make_goal_id("validate_deployment_hypothesis", "Validate deploy X")
        assert a == b
        c = make_goal_id("validate_deployment_hypothesis", "Validate deploy Y")
        assert a != c

    def test_capability_id_deterministic(self):
        a = make_capability_id("collect_pod_lifecycle")
        b = make_capability_id("collect_pod_lifecycle")
        assert a == b
        assert a.startswith("cap:")

    def test_step_id_deterministic(self):
        a = make_step_id("g1", "cap:c1")
        b = make_step_id("g1", "cap:c1")
        assert a == b

    def test_plan_id_stable_across_reordered_input(self):
        s1 = PlanStep.make(goal_id="g1", capability_id="cap:a", reason="")
        s2 = PlanStep.make(goal_id="g2", capability_id="cap:b", reason="")
        p1 = make_plan_id((s1, s2), 80)
        p2 = make_plan_id((s2, s1), 80)
        assert p1 == p2   # sorted internally
        p3 = make_plan_id((s1, s2), 90)
        assert p3 != p1


# ---------------------------------------------------------------------------
# InvestigationGoal
# ---------------------------------------------------------------------------

class TestGoal:
    def test_make_populates_and_clamps(self):
        g = InvestigationGoal.make(
            GoalType.VALIDATE_KUBERNETES_HEALTH,
            "Validate cluster",
            priority=100,
            expected_confidence_gain=500,   # clamped to 100
        )
        assert g.goal_type == "validate_kubernetes_health"
        assert g.expected_confidence_gain == 100
        assert g.schema_version == GOAL_SCHEMA_VERSION

    def test_frozen(self):
        g = InvestigationGoal.make(GoalType.VALIDATE_KUBERNETES_HEALTH, "X")
        with pytest.raises(Exception):
            g.priority = 999

    def test_to_dict_roundtrip(self):
        g = InvestigationGoal.make(GoalType.VALIDATE_KUBERNETES_HEALTH, "X",
                                      completion_criteria=("a", "b"))
        d = g.to_dict()
        assert d["goal_type"] == "validate_kubernetes_health"
        assert d["completion_criteria"] == ("a", "b") or d["completion_criteria"] == ["a", "b"]


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------

class TestCapability:
    def test_make_and_id(self):
        c = Capability.make(
            CapabilityType.COLLECT_LOGS,
            "Collect logs",
            satisfies_goal_types=("collect_root_cause_evidence",),
            typical_confidence_gain=15,
            typical_runtime_ms=6000,
        )
        assert c.capability_id == "cap:collect_logs"
        assert c.typical_confidence_gain == 15
        assert c.schema_version == CAPABILITY_SCHEMA_VERSION

    def test_frozen(self):
        c = Capability.make(CapabilityType.COLLECT_LOGS, "X")
        with pytest.raises(Exception):
            c.description = "y"


# ---------------------------------------------------------------------------
# PlanStep + InvestigationPlan
# ---------------------------------------------------------------------------

class TestPlan:
    def test_step_make(self):
        s = PlanStep.make(
            goal_id="g1", capability_id="cap:collect_logs",
            reason="Baseline logs",
            expected_evidence=("logs",),
            expected_confidence_gain=12, priority=200,
            estimated_runtime_ms=6000,
        )
        assert s.expected_confidence_gain == 12
        assert s.priority == 200
        assert s.estimated_runtime_ms == 6000

    def test_step_frozen(self):
        s = PlanStep.make(goal_id="g", capability_id="cap:x", reason="")
        with pytest.raises(Exception):
            s.reason = "z"

    def test_investigation_plan_defaults(self):
        p = InvestigationPlan(plan_id="p1")
        assert p.is_empty()
        assert p.final_confidence() == 0
        assert p.step_count() == 0
        assert p.goal_count() == 0

    def test_to_dict_is_json_safe(self):
        s = PlanStep.make(goal_id="g", capability_id="cap:x", reason="r",
                           expected_evidence=("e",),
                           success_criteria=("s",),
                           failure_criteria=("f",),
                           dependencies=("d",))
        p = InvestigationPlan(
            plan_id="p", steps=(s,),
            expected_confidence_progression=(60,),
            dependency_graph={s.step_id: ("prev",)},
            estimated_total_cost=1,
            estimated_total_latency_ms=5000,
            initial_confidence=50, target_confidence=80,
        )
        d = p.to_dict()
        s_dump = json.dumps(d)
        d2 = json.loads(s_dump)
        assert d2["plan_id"] == "p"
        assert d2["schema_version"] == PLAN_SCHEMA_VERSION
        assert isinstance(d2["steps"][0]["expected_evidence"], list)


# ---------------------------------------------------------------------------
# PlanContext
# ---------------------------------------------------------------------------

class TestPlanContext:
    def test_defaults_are_empty(self):
        pc = PlanContext()
        assert pc.service == ""
        assert pc.current_confidence == 50
        assert pc.target_confidence == 80
        assert pc.receipts == ()

    def test_with_updates_never_mutates(self):
        pc = PlanContext(service="s", current_confidence=40)
        pc2 = pc.with_updates(current_confidence=60)
        # Original unchanged
        assert pc.current_confidence == 40
        assert pc2.current_confidence == 60
        # Same class
        assert isinstance(pc2, PlanContext)

    def test_frozen(self):
        pc = PlanContext()
        with pytest.raises(Exception):
            pc.service = "x"
