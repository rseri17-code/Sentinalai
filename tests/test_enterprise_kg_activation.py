"""Activation tests for the Enterprise Knowledge Graph runtime module."""
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
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.enterprise_knowledge_graph import (
    ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG,
    ENTERPRISE_KNOWLEDGE_GRAPH_SPEC,
    GRAPH_VERSION,
    enterprise_knowledge_graph_runner,
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
        assert ENTERPRISE_KNOWLEDGE_GRAPH_SPEC.name == "enterprise_knowledge_graph"

    def test_stage(self):
        assert ENTERPRISE_KNOWLEDGE_GRAPH_SPEC.stage == IntelligenceStage.POST_PERSIST

    def test_flag(self):
        assert ENTERPRISE_KNOWLEDGE_GRAPH_SPEC.feature_flag == ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG
        assert ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG == "ENABLE_ENTERPRISE_KNOWLEDGE_GRAPH"

    def test_priority(self):
        assert ENTERPRISE_KNOWLEDGE_GRAPH_SPEC.priority == 900


# ---------------------------------------------------------------------------
# Skip / basic success
# ---------------------------------------------------------------------------

class TestSkip:
    def test_no_receipts_skips(self):
        out = enterprise_knowledge_graph_runner(_ctx(phase_receipts=()))
        assert out["status"] == "skipped"
        assert out["reason"] == "no_phase_receipts"
        assert out["version"] == GRAPH_VERSION


class TestBasicSuccess:
    def test_receipts_without_intelligence_still_build(self):
        receipts = [{"phase_name": "fetch", "metadata": {}}]
        out = enterprise_knowledge_graph_runner(_ctx(phase_receipts=receipts))
        assert out["status"] == "success"
        # Central incident + service = 2 nodes, 1 edge
        assert out["graph_summary"]["node_count"] == 2
        assert out["graph_summary"]["edge_count"] == 1


# ---------------------------------------------------------------------------
# Full transform through runtime
# ---------------------------------------------------------------------------

class TestFullTransform:
    def test_all_six_sources_produce_full_graph(self):
        r = _intel_receipt(
            historical_lookup={
                "service": "checkout", "incident_type": "saturation",
                "resolution_memory_matches": [
                    {"memory_id": "m1", "root_cause_head": "db pool",
                      "confidence": 82, "recorded_at": "x"},
                ],
                "investigation_matches": [
                    {"investigation_id": "inv-old", "created_at": "y"},
                ],
            },
            pattern_recognition={
                "service": "checkout", "incident_type": "saturation",
                "pattern_matches": [
                    {"pattern_id": "p1", "incident_type": "db_pool",
                      "services": ["checkout"], "occurrence_count": 5,
                      "success_count": 4, "success_rate": 0.8,
                      "last_seen": ""},
                ],
            },
            incident_graph_lookup={"service": "checkout",
                                     "related_incident_ids": ["INC_A"]},
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
                                "resolution_action_head": "restart",
                                "outcome": "resolved", "confidence": 0.9,
                                "recorded_at": ""}],
            },
            causal_graph_lookup={
                "service": "checkout", "severity": "high",
                "total_affected": 3,
                "affected": [{"service_id": "ui-web", "probability": 0.9,
                                "propagation_ms": 100, "path": []}],
            },
        )
        out = enterprise_knowledge_graph_runner(
            _ctx(phase_receipts=[r], incident_id="INC1"))
        assert out["status"] == "success"

        summary = out["graph_summary"]
        # A rich mix of node types
        for t in ("service", "incident", "pattern"):
            assert t in summary["node_type_counts"]
        # A rich mix of edge types
        for e in ("affected_by", "historical_failure", "related_incident",
                    "known_pattern", "depends_on", "known_blast_radius"):
            assert e in summary["edge_type_counts"]

        # Full graph payload is present + JSON-safe
        s = json.dumps(out["knowledge_graph"])
        assert json.loads(s)["schema_version"] == GRAPH_VERSION


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "enterprise_knowledge_graph")
        assert entry.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt(
                                     pattern_recognition={
                                         "service": "checkout",
                                         "incident_type": "saturation",
                                         "pattern_matches": [],
                                     })]))
        entry = next(r for r in results if r.name == "enterprise_knowledge_graph")
        assert entry.status == "success"
        assert entry.metadata["version"] == GRAPH_VERSION


# ---------------------------------------------------------------------------
# Receipt lift
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_metadata_flows_to_receipt(self, monkeypatch):
        monkeypatch.setenv(ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG, "true")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("persist") as _r:
            results = rt.run_stage(
                IntelligenceStage.POST_PERSIST,
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
                       if e["name"] == "enterprise_knowledge_graph")
        assert entry["status"] == "success"
        assert entry["metadata"]["graph_summary"]["node_count"] >= 2


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_builder_failure_captured_by_runtime(self, monkeypatch):
        monkeypatch.setenv(ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from sentinel_core.models import knowledge_graph as _kg
        with patch.object(_kg.KnowledgeGraphBuilder,
                           "from_intelligence_context",
                           side_effect=RuntimeError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "enterprise_knowledge_graph")
        assert entry.status == "failed"
        assert entry.error_type == "RuntimeError"

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
        out = enterprise_knowledge_graph_runner(_ctx(phase_receipts=[weird]))
        assert out["status"] == "success"
        # The one good pattern survived and became a PATTERN node
        assert out["graph_summary"]["node_type_counts"].get("pattern", 0) == 1


# ---------------------------------------------------------------------------
# Deterministic serialization
# ---------------------------------------------------------------------------

class TestDeterministic:
    def test_same_receipts_produce_byte_identical_graph(self):
        r = _intel_receipt(pattern_recognition={
            "service": "checkout", "incident_type": "t",
            "pattern_matches": [{"pattern_id": "p", "incident_type": "t",
                                  "occurrence_count": 2, "success_count": 1,
                                  "success_rate": 0.5, "last_seen": ""}],
        })
        o1 = enterprise_knowledge_graph_runner(_ctx(phase_receipts=[r]))
        o2 = enterprise_knowledge_graph_runner(_ctx(phase_receipts=[r]))
        assert json.dumps(o1["knowledge_graph"], sort_keys=True) \
            == json.dumps(o2["knowledge_graph"], sort_keys=True)


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_registered(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("enterprise_knowledge_graph")

    def test_in_post_persist_plan(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_PERSIST)
        names = [s.name for s in specs]
        assert "enterprise_knowledge_graph" in names
        # After intelligence_context_persister (priority 800)
        assert names.index("enterprise_knowledge_graph") > names.index(
            "intelligence_context_persister"
        )

    def test_agent_source_untouched(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        # New activation must not add any reference to itself in agent.py
        assert "enterprise_knowledge_graph" not in src
        assert "install_default_modules(_intel)" in src
