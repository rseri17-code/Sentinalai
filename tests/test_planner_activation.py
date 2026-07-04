"""Activation tests for the Planner runtime module."""
from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    RuntimeContext,
)
from supervisor.deterministic_planner import (
    PLANNER_FEATURE_FLAG,
    PLANNER_SPEC,
    planner_runner,
)
from supervisor.intelligence_modules import install_default_modules


@dataclass
class _FakeCres:
    incident_type: str = "saturation"


def _intel_receipt(**entries) -> dict:
    return {
        "phase_name": "classify",
        "metadata": {
            "intelligence": [
                {"name": name, "status": "success",
                  "metadata": payload, "warnings": []}
                for name, payload in entries.items()
            ],
        },
    }


def _result() -> dict:
    return {
        "root_cause":  "checkout DB pool exhausted",
        "remediation": {"immediate_action": "scale pool"},
    }


def _ctx(*, investigation_id="inv-INC1", incident_id="INC1",
         service="checkout", incident_type="saturation",
         phase_receipts=(), result=None):
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_PERSIST,
        fetch_out={
            "incident": {"incident_id": incident_id, "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
        result=result or _result(),
        phase_receipts=tuple(phase_receipts),
    )


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

class TestSpec:
    def test_name(self):
        assert PLANNER_SPEC.name == "planner"

    def test_stage(self):
        assert PLANNER_SPEC.stage == IntelligenceStage.POST_PERSIST

    def test_flag(self):
        assert PLANNER_SPEC.feature_flag == PLANNER_FEATURE_FLAG
        assert PLANNER_FEATURE_FLAG == "ENABLE_PLANNER"

    def test_priority_is_after_all_persisters(self):
        assert PLANNER_SPEC.priority == 950


# ---------------------------------------------------------------------------
# Skip
# ---------------------------------------------------------------------------

class TestSkip:
    def test_no_receipts_skips(self):
        out = planner_runner(_ctx(phase_receipts=()))
        assert out["status"] == "skipped"
        assert out["reason"] == "no_phase_receipts"


# ---------------------------------------------------------------------------
# Full transform through runtime
# ---------------------------------------------------------------------------

class TestFullTransform:
    def test_basic_success(self):
        out = planner_runner(_ctx(
            phase_receipts=[_intel_receipt(pattern_recognition={
                "service": "checkout", "incident_type": "saturation",
                "pattern_matches": [{"pattern_id": "p",
                                      "incident_type": "db_pool",
                                      "occurrence_count": 3,
                                      "success_count": 2,
                                      "success_rate": 0.66,
                                      "last_seen": ""}],
            })],
        ))
        assert out["status"] == "success"
        # Plan present + summary populated
        assert "plan" in out
        assert out["plan_summary"]["step_count"] > 0
        assert out["plan_summary"]["goal_count"] > 0
        # Deterministic id
        assert out["plan"]["plan_id"] == out["plan_summary"]["plan_id"]

    def test_full_receipt_yields_full_plan(self):
        # Multiple goal-driving signals + minimal DC confidence uplift so
        # the planner has room to schedule several steps before reaching
        # target confidence (correct early-stop behavior is verified
        # separately in the builder unit tests).
        r = _intel_receipt(
            pattern_recognition={
                "service": "checkout", "incident_type": "saturation",
                "pattern_matches": [{"pattern_id": "p",
                                      "incident_type": "db_pool",
                                      "occurrence_count": 1,   # <2 → NOT recurring
                                      "success_count": 0,
                                      "success_rate": 0.0,     # <0.5 → no uplift
                                      "last_seen": ""}],
            },
            causal_graph_lookup={"service": "checkout",
                                    "severity": "high",
                                    "total_affected": 3,
                                    "affected": []},
        )
        out = planner_runner(_ctx(phase_receipts=[r]))
        assert out["status"] == "success"
        # Multiple goals derived (root_cause + storage + blast_radius)
        assert out["plan_summary"]["goal_count"] >= 3
        # DC confidence starts at 50 (no uplift) → target 80 → room for
        # at least 2 steps.
        assert out["plan_summary"]["step_count"] >= 2

    def test_receipt_without_intelligence_still_succeeds(self):
        # Baseline path — no intelligence entries
        out = planner_runner(_ctx(phase_receipts=[
            {"phase_name": "fetch", "metadata": {}}
        ]))
        assert out["status"] == "success"


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off_bypasses(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(PLANNER_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(PLANNER_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "planner")
        assert entry.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(PLANNER_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt(
                                     pattern_recognition={
                                         "service": "checkout",
                                         "incident_type": "saturation",
                                         "pattern_matches": [],
                                     })]))
        entry = next(r for r in results if r.name == "planner")
        assert entry.status == "success"


# ---------------------------------------------------------------------------
# Receipt lift
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_plan_summary_flows_to_receipt(self, monkeypatch):
        monkeypatch.setenv(PLANNER_FEATURE_FLAG, "true")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("persist") as _r:
            results = rt.run_stage(
                IntelligenceStage.POST_PERSIST,
                _ctx(phase_receipts=[_intel_receipt(
                    causal_graph_lookup={"service": "checkout",
                                            "severity": "high",
                                            "total_affected": 3,
                                            "affected": []},
                )]),
            )
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        entry = next(e for e in col.to_list()[0]["metadata"]["intelligence"]
                       if e["name"] == "planner")
        assert entry["status"] == "success"
        assert entry["metadata"]["plan_summary"]["step_count"] > 0


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_builder_failure_captured(self, monkeypatch):
        monkeypatch.setenv(PLANNER_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.deterministic_planner import planner_builder as _pb
        with patch.object(_pb.PlannerBuilder, "build",
                           side_effect=RuntimeError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "planner")
        assert entry.status == "failed"
        assert entry.error_type == "RuntimeError"


# ---------------------------------------------------------------------------
# Deterministic serialization
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_receipts_yield_byte_identical_plan(self):
        r = _intel_receipt(pattern_recognition={
            "service": "checkout", "incident_type": "saturation",
            "pattern_matches": [{"pattern_id": "p",
                                  "incident_type": "db_pool",
                                  "occurrence_count": 2,
                                  "success_count": 1,
                                  "success_rate": 0.5,
                                  "last_seen": ""}],
        })
        out1 = planner_runner(_ctx(phase_receipts=[r]))
        out2 = planner_runner(_ctx(phase_receipts=[r]))
        assert json.dumps(out1["plan"], sort_keys=True) \
            == json.dumps(out2["plan"], sort_keys=True)


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_registered(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("planner")

    def test_in_post_persist_plan_after_kg(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_PERSIST)
        names = [s.name for s in specs]
        assert "planner" in names
        # Planner runs AFTER enterprise_knowledge_graph
        assert names.index("planner") > names.index("enterprise_knowledge_graph")

    def test_agent_source_untouched(self):
        """Planner activation MUST NOT touch supervisor/agent.py."""
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "planner_runner" not in src
        assert "PLANNER_SPEC" not in src
        assert "install_default_modules(_intel)" in src


# ---------------------------------------------------------------------------
# Malformed input tolerance
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_malformed_intelligence_isolated(self):
        weird = {
            "phase_name": "classify",
            "metadata": {
                "intelligence": [
                    "not-a-dict",
                    {"name": "some_module", "metadata": "also-bad"},
                    None,
                    {"name": "pattern_recognition",
                      "metadata": {"pattern_matches": [{"pattern_id": "ok",
                                                          "incident_type": "t",
                                                          "occurrence_count": 2,
                                                          "success_count": 1,
                                                          "success_rate": 0.5,
                                                          "last_seen": ""}]}},
                ],
            },
        }
        out = planner_runner(_ctx(phase_receipts=[weird]))
        assert out["status"] == "success"


# ---------------------------------------------------------------------------
# Non-mutation
# ---------------------------------------------------------------------------

class TestNonMutation:
    def test_runner_does_not_modify_ctx_fields(self):
        ctx = _ctx(phase_receipts=[_intel_receipt(pattern_recognition={
            "service": "checkout", "incident_type": "saturation",
            "pattern_matches": [],
        })])
        # Snapshot immutable-facing fields
        original_incident_id = ctx.fetch_out["incident"]["incident_id"]
        original_service = ctx.fetch_out["service"]
        original_incident_type = ctx.cres.incident_type
        original_receipts_len = len(ctx.phase_receipts)
        planner_runner(ctx)
        assert ctx.fetch_out["incident"]["incident_id"] == original_incident_id
        assert ctx.fetch_out["service"] == original_service
        assert ctx.cres.incident_type == original_incident_type
        assert len(ctx.phase_receipts) == original_receipts_len


# ---------------------------------------------------------------------------
# Investigate() untouched — end-to-end contract
# ---------------------------------------------------------------------------

class TestInvestigateUntouched:
    def test_investigate_signature_unchanged(self):
        """The mission rule 'Do NOT modify investigate()' — verify the
        signature and file are unchanged."""
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "def investigate(" in src
        # No planner-specific hooks inline
        assert "PlannerBuilder" not in src
        assert "PLANNER_SPEC" not in src
