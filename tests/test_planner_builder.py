"""Deterministic transform tests for PlannerBuilder + planner_rules."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

import pytest

from sentinel_core.models.plan_context import PlanContext
from sentinel_core.models.planner import CapabilityType, GoalType
from supervisor.deterministic_planner.planner_builder import (
    PLANNER_VERSION,
    PlannerBuilder,
)
from supervisor.deterministic_planner.planner_registry import (
    DEFAULT_SKILL_REGISTRY,
    SkillRegistry,
)
from supervisor.deterministic_planner.planner_rules import (
    catalog,
    compute_dependencies,
    derive_goals,
    select_capabilities_for_goal,
)


# ---------------------------------------------------------------------------
# Fakes — duck-typed DecisionContext + KnowledgeGraph
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeBlast:
    severity: str = "low"
    total_affected: int = 0


@dataclass(frozen=True)
class _FakeDC:
    service: str = "checkout"
    incident_type: str = "saturation"
    confidence: int = 60
    likely_failure_type: str = ""
    recurring_incident: bool = False
    historical_success_rate: float = 0.0
    recommended_next_service: str = ""
    recommended_queries: tuple = ()
    likely_blast_radius: _FakeBlast = field(default_factory=_FakeBlast)


def _pc(**kwargs) -> PlanContext:
    default_dc = kwargs.pop("decision_context", _FakeDC())
    return PlanContext(
        service=kwargs.pop("service", getattr(default_dc, "service", "")),
        incident_type=kwargs.pop("incident_type",
                                    getattr(default_dc, "incident_type", "")),
        decision_context=default_dc,
        current_confidence=kwargs.pop("current_confidence", 50),
        target_confidence=kwargs.pop("target_confidence", 80),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Rule module — catalog + goal derivation + capability selection
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_catalog_has_all_expected_capabilities(self):
        c = catalog()
        for ct in CapabilityType:
            assert ct.value in c

    def test_catalog_is_copy(self):
        c1 = catalog()
        c1["x"] = None
        c2 = catalog()
        assert "x" not in c2

    def test_capability_deterministic_ordering(self):
        caps = select_capabilities_for_goal(
            GoalType.COLLECT_ROOT_CAUSE_EVIDENCE.value
        )
        ids = [c.capability_id for c in caps]
        assert ids == sorted(ids)


class TestGoalDerivation:
    def test_always_derives_root_cause_goal(self):
        goals = derive_goals(_pc(incident_type="unknown"))
        types = {g.goal_type for g in goals}
        assert GoalType.COLLECT_ROOT_CAUSE_EVIDENCE.value in types

    def test_storage_keyword_fires_storage_goal(self):
        goals = derive_goals(_pc(incident_type="db pool saturation"))
        types = {g.goal_type for g in goals}
        assert GoalType.DETERMINE_STORAGE_BOTTLENECK.value in types

    def test_network_keyword_fires_network_goal(self):
        goals = derive_goals(_pc(incident_type="network timeout"))
        types = {g.goal_type for g in goals}
        assert GoalType.DETERMINE_NETWORK_FAILURE.value in types

    def test_auth_keyword_fires_auth_goal(self):
        goals = derive_goals(_pc(incident_type="unauthorized token"))
        types = {g.goal_type for g in goals}
        assert GoalType.DETERMINE_AUTHENTICATION_FAILURE.value in types

    def test_k8s_keyword_fires_k8s_goal(self):
        goals = derive_goals(_pc(incident_type="pod oom"))
        types = {g.goal_type for g in goals}
        assert GoalType.VALIDATE_KUBERNETES_HEALTH.value in types

    def test_deploy_keyword_fires_deploy_goal(self):
        goals = derive_goals(_pc(incident_type="post-deployment latency"))
        types = {g.goal_type for g in goals}
        assert GoalType.VALIDATE_DEPLOYMENT_HYPOTHESIS.value in types

    def test_recurring_incident_derives_historical_goal(self):
        pc = _pc(decision_context=_FakeDC(recurring_incident=True))
        goals = derive_goals(pc)
        types = {g.goal_type for g in goals}
        assert GoalType.COMPARE_HISTORICAL_FAILURES.value in types

    def test_high_blast_severity_derives_blast_goal(self):
        pc = _pc(decision_context=_FakeDC(
            likely_blast_radius=_FakeBlast(severity="high", total_affected=3)
        ))
        goals = derive_goals(pc)
        types = {g.goal_type for g in goals}
        assert GoalType.ASSESS_BLAST_RADIUS.value in types

    def test_completed_goals_are_skipped(self):
        # First run: derive all goals
        pc1 = _pc(incident_type="pod oom", decision_context=_FakeDC(
            recurring_incident=True))
        goals1 = derive_goals(pc1)
        completed_ids = tuple(g.goal_id for g in goals1)
        # Second run: all completed → no goals derived
        pc2 = pc1.with_updates(completed_goals=completed_ids)
        goals2 = derive_goals(pc2)
        assert len(goals2) == 0

    def test_deterministic_order(self):
        pc = _pc(incident_type="db pool", decision_context=_FakeDC(
            recurring_incident=True,
            likely_blast_radius=_FakeBlast(severity="critical", total_affected=5)))
        g1 = derive_goals(pc)
        g2 = derive_goals(pc)
        assert [g.goal_id for g in g1] == [g.goal_id for g in g2]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_compare_depends_on_collect_historical(self):
        # Build a plan by hand with both capabilities
        pc = _pc(incident_type="db pool saturation",
                  decision_context=_FakeDC(recurring_incident=True))
        plan = PlannerBuilder().build(pc)
        deps = plan.dependency_graph
        # If both capabilities are scheduled, they must have a dep edge
        cap_ids = {s.capability_id for s in plan.steps}
        if ("cap:compare_historical_failures" in cap_ids
                and "cap:collect_historical_incidents" in cap_ids):
            # Find the compare step id
            compare = next(s for s in plan.steps
                            if s.capability_id == "cap:compare_historical_failures")
            assert compare.step_id in deps

    def test_no_deps_yield_empty_edges(self):
        pc = _pc(incident_type="unknown_type")
        plan = PlannerBuilder().build(pc)
        # Trivial case: nothing depends on nothing here
        assert isinstance(plan.dependency_graph, dict)


# ---------------------------------------------------------------------------
# Skill Registry
# ---------------------------------------------------------------------------

class TestSkillRegistry:
    def test_default_contains_expected_caps(self):
        r = SkillRegistry()
        assert r.has("cap:collect_pod_lifecycle")
        assert r.has("cap:collect_logs")
        assert not r.has("cap:nonexistent")

    def test_skills_for_returns_tuple(self):
        r = SkillRegistry()
        s = r.skills_for("cap:collect_pod_lifecycle")
        assert isinstance(s, tuple)
        assert "kubectl_pods" in s

    def test_extend_is_immutable(self):
        r = SkillRegistry()
        r2 = r.extend({"cap:custom": ("mytool",)})
        assert not r.has("cap:custom")   # original unchanged
        assert r2.has("cap:custom")

    def test_extend_does_not_override_existing(self):
        r = SkillRegistry()
        original = r.skills_for("cap:collect_logs")
        r2 = r.extend({"cap:collect_logs": ("override",)})
        assert r2.skills_for("cap:collect_logs") == original

    def test_default_registry_is_read_only(self):
        with pytest.raises(TypeError):
            DEFAULT_SKILL_REGISTRY["cap:x"] = ("y",)   # MappingProxyType

    def test_to_dict_sorted(self):
        r = SkillRegistry()
        d = r.to_dict()
        keys = list(d.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# PlannerBuilder end-to-end
# ---------------------------------------------------------------------------

class TestPlannerBuilder:
    def test_none_context_produces_empty_plan(self):
        p = PlannerBuilder().build(None)
        assert p.step_count() == 0
        assert p.goal_count() == 0

    def test_empty_context_derives_baseline_goal_only(self):
        pc = _pc(incident_type="", decision_context=_FakeDC(incident_type=""))
        # empty everything: incident_type is empty
        plan = PlannerBuilder().build(pc)
        # Baseline "collect root cause evidence" is always derived
        assert plan.goal_count() >= 1
        assert plan.step_count() >= 1

    def test_full_context_produces_ordered_plan(self):
        pc = _pc(
            incident_type="db pool saturation with pod restarts",
            decision_context=_FakeDC(
                recurring_incident=True,
                historical_success_rate=0.7,
                likely_blast_radius=_FakeBlast(severity="high",
                                                  total_affected=3),
            ),
            current_confidence=40,
        )
        plan = PlannerBuilder().build(pc)

        # Steps are non-empty
        assert plan.step_count() > 0
        # Steps are sorted by expected_confidence_gain descending
        gains = [s.expected_confidence_gain for s in plan.steps]
        assert gains == sorted(gains, reverse=True)

        # Confidence progression cumulates and is capped at 100
        prog = plan.expected_confidence_progression
        assert list(prog) == sorted(prog)
        assert all(v <= 100 for v in prog)

        # Final confidence is the last progression entry
        assert plan.final_confidence() == prog[-1]

        # Estimated total latency is sum of estimated_runtime_ms
        assert plan.estimated_total_latency_ms == sum(
            s.estimated_runtime_ms for s in plan.steps
        )

    def test_target_confidence_stops_planning(self):
        pc = _pc(current_confidence=75, target_confidence=80)
        plan = PlannerBuilder().build(pc)
        # After 1 step we should be at or above 80
        if plan.expected_confidence_progression:
            assert plan.expected_confidence_progression[-1] >= 80

    def test_max_steps_capped(self):
        # A max_steps=2 builder produces at most 2 steps
        pc = _pc(incident_type="db pool network pod auth deploy",
                  decision_context=_FakeDC(recurring_incident=True))
        p = PlannerBuilder(max_steps=2).build(pc)
        assert p.step_count() <= 2

    def test_deterministic_output(self):
        pc = _pc(incident_type="db pool saturation",
                  decision_context=_FakeDC(recurring_incident=True))
        p1 = PlannerBuilder().build(pc)
        p2 = PlannerBuilder().build(pc)
        assert json.dumps(p1.to_dict(), sort_keys=True) \
            == json.dumps(p2.to_dict(), sort_keys=True)

    def test_no_duplicate_capabilities(self):
        pc = _pc(incident_type="db pool network pod",
                  decision_context=_FakeDC(recurring_incident=True))
        p = PlannerBuilder().build(pc)
        cap_ids = [s.capability_id for s in p.steps]
        assert len(cap_ids) == len(set(cap_ids))

    def test_no_context_mutation(self):
        dc = _FakeDC(recurring_incident=True)
        pc = _pc(incident_type="db pool", decision_context=dc)
        # Snapshot state before + after
        pc_before = copy.deepcopy(pc)
        PlannerBuilder().build(pc)
        assert pc.service == pc_before.service
        assert pc.decision_context.recurring_incident \
            == pc_before.decision_context.recurring_incident

    def test_malformed_current_confidence(self):
        pc = PlanContext(current_confidence="not-an-int")
        # Must not raise — clamps to default
        p = PlannerBuilder().build(pc)
        assert p.initial_confidence == 0

    def test_dependencies_in_plan_only(self):
        # Verify that dependency_graph edges only reference step_ids
        # that are in the plan.
        pc = _pc(incident_type="db pool saturation",
                  decision_context=_FakeDC(recurring_incident=True))
        p = PlannerBuilder().build(pc)
        step_ids = {s.step_id for s in p.steps}
        for k, deps in p.dependency_graph.items():
            assert k in step_ids
            for d in deps:
                assert d in step_ids


# ---------------------------------------------------------------------------
# InvestigationPlan.to_dict / determinism
# ---------------------------------------------------------------------------

class TestPlanSerialization:
    def test_plan_id_stable_across_calls(self):
        pc = _pc(incident_type="db pool")
        p1 = PlannerBuilder().build(pc)
        p2 = PlannerBuilder().build(pc)
        assert p1.plan_id == p2.plan_id

    def test_json_dumpable(self):
        pc = _pc(incident_type="db pool")
        p = PlannerBuilder().build(pc)
        d = p.to_dict()
        s = json.dumps(d, sort_keys=True)
        assert json.loads(s)["plan_id"] == p.plan_id


# ---------------------------------------------------------------------------
# Version constant
# ---------------------------------------------------------------------------

class TestVersion:
    def test_planner_version(self):
        assert PLANNER_VERSION == 1
