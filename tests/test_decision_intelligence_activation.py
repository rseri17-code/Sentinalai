"""DecisionIntelligence activation tests.

Verifies the runtime module that transforms IntelligenceContext into
DecisionContext at POST_COLLECT. All tests are pure — no store I/O.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    RuntimeContext,
)
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.decision_intelligence import (
    DECISION_INTELLIGENCE_FEATURE_FLAG,
    DECISION_INTELLIGENCE_SPEC,
    DECISION_VERSION,
    decision_intelligence_runner,
)


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


def _ctx(*, investigation_id="inv-INC1", service="checkout",
         incident_type="saturation", phase_receipts=()):
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_COLLECT,
        fetch_out={"incident": {"incident_id": "INC1",
                                  "affected_service": service},
                     "service": service},
        cres=_FakeCres(incident_type=incident_type),
        phase_receipts=tuple(phase_receipts),
    )


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

class TestSpec:
    def test_name(self):
        assert DECISION_INTELLIGENCE_SPEC.name == "decision_intelligence"

    def test_stage_is_post_collect(self):
        assert DECISION_INTELLIGENCE_SPEC.stage == IntelligenceStage.POST_COLLECT

    def test_feature_flag(self):
        assert DECISION_INTELLIGENCE_SPEC.feature_flag == DECISION_INTELLIGENCE_FEATURE_FLAG
        assert DECISION_INTELLIGENCE_FEATURE_FLAG == "ENABLE_DECISION_INTELLIGENCE"

    def test_priority(self):
        assert DECISION_INTELLIGENCE_SPEC.priority == 100


# ---------------------------------------------------------------------------
# Skip / empty
# ---------------------------------------------------------------------------

class TestSkip:
    def test_no_receipts_skips(self):
        out = decision_intelligence_runner(_ctx(phase_receipts=()))
        assert out["status"] == "skipped"
        assert out["reason"] == "no_phase_receipts"
        assert out["version"] == DECISION_VERSION


class TestSuccessWithNoIntelligenceEntries:
    def test_receipt_without_intelligence_metadata_still_success(self):
        receipts = [{"phase_name": "fetch", "metadata": {}}]
        out = decision_intelligence_runner(_ctx(phase_receipts=receipts))
        assert out["status"] == "success"
        # Full decision context present
        assert "decision_context" in out
        assert "decision_summary" in out
        # All 6 modules are gaps because nothing was seen
        assert len(out["evidence_gaps"]) == 6


# ---------------------------------------------------------------------------
# Full transform through the runtime
# ---------------------------------------------------------------------------

class TestFullTransform:
    def test_all_six_sources_produce_full_decision(self):
        r = _intel_receipt(
            historical_lookup={
                "service": "checkout", "incident_type": "saturation",
                "resolution_memory_matches": [
                    {"memory_id": "m", "root_cause_head": "db pool",
                      "confidence": 82, "recorded_at": "x"},
                ],
                "investigation_matches": [
                    {"investigation_id": "i", "created_at": "y"},
                ],
            },
            pattern_recognition={
                "service": "checkout", "incident_type": "saturation",
                "pattern_matches": [
                    {"pattern_id": "p", "incident_type": "db_pool_exhaustion",
                      "services": ["checkout"], "occurrence_count": 5,
                      "success_count": 4, "success_rate": 0.8,
                      "last_seen": "z"},
                ],
            },
            incident_graph_lookup={"service": "checkout",
                                     "related_incident_ids": ["INC_A", "INC_B"]},
            dependency_graph_lookup={
                "service": "checkout",
                "upstream": [{"source_service": "checkout",
                                "target_service": "db",
                                "dep_type": "runtime", "strength": 0.9}],
                "downstream": [{"source_service": "cart-api",
                                  "target_service": "checkout",
                                  "dep_type": "runtime", "strength": 0.8}],
                "affected_services": ["cart-api"],
            },
            episodic_memory_lookup={
                "service": "checkout", "incident_type": "saturation",
                "episodes": [{"episode_id": "e", "incident_id": "INC1",
                                "service": "checkout",
                                "incident_type": "saturation",
                                "root_cause_head": "cause",
                                "resolution_action_head": "action",
                                "outcome": "resolved", "confidence": 0.9,
                                "recorded_at": ""}],
            },
            causal_graph_lookup={
                "service": "checkout", "severity": "high",
                "total_affected": 3,
                "affected": [{"service_id": "cart-api", "probability": 0.9,
                                "propagation_ms": 100, "path": ["checkout",
                                                                  "cart-api"]}],
            },
        )
        out = decision_intelligence_runner(_ctx(phase_receipts=[r]))
        assert out["status"] == "success"

        dc = out["decision_context"]
        assert dc["service"] == "checkout"
        # Top recurring pattern's incident_type wins
        assert dc["likely_failure_type"] == "db_pool_exhaustion"
        assert dc["recurring_incident"] is True
        assert dc["historical_success_rate"] == 0.8
        assert dc["likely_blast_radius"]["severity"] == "high"
        assert dc["likely_blast_radius"]["top_service"] == "cart-api"
        assert dc["recommended_next_service"] == "cart-api"
        # Investigation priority — high because severity=high
        assert dc["investigation_priority"] == "high"
        # Confidence uplift from all signals
        assert dc["confidence"] > 60
        # No evidence gaps — all six were seen
        assert dc["evidence_gaps"] == []

        # Compact summary
        assert out["decision_summary"]["confidence"] == dc["confidence"]
        assert out["decision_summary"]["investigation_priority"] == "high"
        assert out["decision_summary"]["blast_radius_severity"] == "high"


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(DECISION_INTELLIGENCE_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_COLLECT,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(DECISION_INTELLIGENCE_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_COLLECT,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "decision_intelligence")
        assert entry.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(DECISION_INTELLIGENCE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_COLLECT,
                                 _ctx(phase_receipts=[_intel_receipt(
                                     pattern_recognition={
                                         "service": "checkout",
                                         "incident_type": "saturation",
                                         "pattern_matches": [],
                                     })]))
        entry = next(r for r in results if r.name == "decision_intelligence")
        assert entry.status == "success"
        assert entry.metadata["version"] == DECISION_VERSION


# ---------------------------------------------------------------------------
# Receipt lift
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_decision_context_flows_to_receipt(self, monkeypatch):
        monkeypatch.setenv(DECISION_INTELLIGENCE_FEATURE_FLAG, "true")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("collect") as _r:
            results = rt.run_stage(
                IntelligenceStage.POST_COLLECT,
                _ctx(phase_receipts=[_intel_receipt(
                    causal_graph_lookup={"service": "checkout",
                                            "severity": "critical",
                                            "total_affected": 5,
                                            "affected": []},
                )]),
            )
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        entry = next(e for e in col.to_list()[0]["metadata"]["intelligence"]
                       if e["name"] == "decision_intelligence")
        assert entry["status"] == "success"
        assert entry["metadata"]["decision_summary"]["investigation_priority"] == "critical"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_context_construction_failure_captured(self, monkeypatch):
        monkeypatch.setenv(DECISION_INTELLIGENCE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from sentinel_core.models import intel_context as _ic
        with patch.object(_ic.IntelligenceContext, "from_receipts",
                           side_effect=RuntimeError("broken")):
            results = rt.run_stage(IntelligenceStage.POST_COLLECT,
                                     _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "decision_intelligence")
        assert entry.status == "failed"
        assert entry.error_type == "RuntimeError"


# ---------------------------------------------------------------------------
# Deterministic serialization
# ---------------------------------------------------------------------------

class TestDeterministicSerialization:
    def test_same_receipts_yield_identical_decision_context(self):
        r = _intel_receipt(pattern_recognition={
            "service": "checkout", "incident_type": "t",
            "pattern_matches": [{"pattern_id": "p", "incident_type": "t",
                                  "occurrence_count": 2, "success_count": 1,
                                  "success_rate": 0.5, "last_seen": ""}],
        })
        out1 = decision_intelligence_runner(_ctx(phase_receipts=[r]))
        out2 = decision_intelligence_runner(_ctx(phase_receipts=[r]))
        # Byte-identical decision_context payload
        assert json.dumps(out1["decision_context"], sort_keys=True) \
            == json.dumps(out2["decision_context"], sort_keys=True)


# ---------------------------------------------------------------------------
# Malformed / missing intelligence handling
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_malformed_intelligence_entries_isolated(self):
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
                                                          "occurrence_count": 3,
                                                          "success_count": 2,
                                                          "success_rate": 0.66,
                                                          "last_seen": ""}]}},
                ],
            },
        }
        out = decision_intelligence_runner(_ctx(phase_receipts=[weird]))
        assert out["status"] == "success"
        # Good pattern survived and drove recurring signal
        assert out["decision_context"]["recurring_incident"] is True


# ---------------------------------------------------------------------------
# Agent + ordering wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_registered_via_install_default_modules(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("decision_intelligence")

    def test_module_appears_in_post_collect_plan(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_COLLECT)
        assert any(s.name == "decision_intelligence" for s in specs)

    def test_agent_source_untouched_by_this_activation(self):
        """The activation must not require any change to agent.py beyond
        the phase_receipts snapshot that was already added by the persister
        mission — investigate() signature + LLM prompt + evidence still
        unchanged."""
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "install_default_modules(_intel)" in src
        # Our new module name is NOT referenced directly in agent.py —
        # registration is via __init__.py.
        assert "decision_intelligence" not in src


# ---------------------------------------------------------------------------
# RuntimeContext back-compat — phase_receipts is now snapshotted at all stages
# ---------------------------------------------------------------------------

class TestPhaseReceiptsUnconditional:
    def test_agent_hook_snapshots_receipts_unconditionally(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        # The hook must include the unconditional setdefault so POST_COLLECT
        # decision_intelligence sees the earlier receipts.
        assert "fields.setdefault(\"phase_receipts\", tuple(_phase_receipts.to_list()))" in src

    def test_runtime_context_still_defaults_to_empty(self):
        # Default is still ()
        c = RuntimeContext(investigation_id="x",
                            stage=IntelligenceStage.POST_COLLECT)
        assert c.phase_receipts == ()
