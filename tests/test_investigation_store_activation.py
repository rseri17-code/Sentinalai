"""Phase 21 — InvestigationStore activation tests.

Verifies that the dormant ``intelligence.investigation_store`` cluster
becomes an active per-investigation write path via the Phase 19
Intelligence Runtime, without modifying InvestigationStore or
EvidenceGraph themselves.

Coverage per the mission spec:
- Flag off → no behavior change (byte-identical)
- Master on + module off → no write (status=skipped)
- Successful new write → graph file + index entry produced
- Duplicate investigation_id → deduplicated
- Write failure is non-fatal (runtime failure isolation catches it)
- Receipt metadata includes module=investigation_store + status +
  record_id + deduplicated + elapsed_ms (+ error_type on failure)
- DecisionTrace reference included when present in aout
- ResolutionMemory reference included when Phase 20 wrote first
- Replay path never invokes the runner
- Dependency ordering: resolution_memory runs BEFORE investigation_store
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
    ModuleSpec,
    RuntimeContext,
)
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.investigation_store import (
    INVESTIGATION_STORE_FEATURE_FLAG,
    INVESTIGATION_STORE_SPEC,
    WRITE_VERSION,
    investigation_store_runner,
)
from supervisor.intelligence_modules.resolution_memory import (
    RESOLUTION_MEMORY_FEATURE_FLAG,
)


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------

@dataclass
class _FakeCres:
    incident_type: str = "saturation"


@dataclass
class _FakeAout:
    evidence: dict
    decision_trace_meta: dict


def _ctx(*, investigation_id="inv-INC1", result=None, evidence=None,
        service="checkout", incident_type="saturation", incident_id="INC1",
        decision_trace_meta=None):
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_PERSIST,
        fetch_out={
            "incident": {"incident_id": incident_id, "summary": "x",
                          "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
        aout=_FakeAout(
            evidence=evidence or {"logs": [], "metrics": {}},
            decision_trace_meta=decision_trace_meta or {},
        ),
        result=result or {
            "incident_id": incident_id,
            "root_cause":  "checkout DB pool exhaustion",
            "confidence":  78,
            "reasoning":   "reasoning",
            "remediation": {"immediate_action": "scale pool"},
            "_evidence_snapshot": {"logs": True, "metrics": True},
        },
    )


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    """Isolate INVESTIGATIONS_DIR + OPS_DB_PATH per test."""
    monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path / "investigations"))
    monkeypatch.setenv("OPS_DB_PATH", str(tmp_path / "ops_intelligence.db"))
    monkeypatch.setenv("OPS_DB_ENABLED", "true")
    import database.ops_persistence as _ops
    monkeypatch.setattr(_ops, "_instance", None, raising=False)
    (tmp_path / "investigations").mkdir()


# ---------------------------------------------------------------------------
# Spec metadata
# ---------------------------------------------------------------------------

class TestSpec:
    def test_stage_is_post_persist(self):
        assert INVESTIGATION_STORE_SPEC.stage == IntelligenceStage.POST_PERSIST

    def test_has_feature_flag(self):
        assert INVESTIGATION_STORE_SPEC.feature_flag == INVESTIGATION_STORE_FEATURE_FLAG
        assert INVESTIGATION_STORE_FEATURE_FLAG == "ENABLE_INVESTIGATION_STORE_WRITE"

    def test_depends_on_resolution_memory(self):
        assert "resolution_memory" in INVESTIGATION_STORE_SPEC.dependencies

    def test_priority_after_resolution_memory(self):
        """RM priority=100, IS priority=200 — RM runs first even without deps."""
        assert INVESTIGATION_STORE_SPEC.priority > 100


# ---------------------------------------------------------------------------
# Successful write
# ---------------------------------------------------------------------------

class TestSuccessfulWrite:
    def test_produces_graph_json_file(self, tmp_path):
        out = investigation_store_runner(_ctx(investigation_id="inv-w-1"))
        assert out["status"] == "success"
        assert out["record_id"] == "inv-w-1"
        assert out["version"] == WRITE_VERSION
        graph_path = tmp_path / "investigations" / "inv-w-1.json"
        assert graph_path.exists()

    def test_graph_content_carries_result_summary(self, tmp_path):
        out = investigation_store_runner(_ctx(
            investigation_id="inv-w-2",
            result={
                "root_cause": "checkout pool exhausted",
                "confidence": 87,
                "reasoning": "many DB connections",
                "_evidence_snapshot": {"logs": True},
                "remediation": {"immediate_action": "scale"},
            },
        ))
        assert out["status"] == "success"
        data = json.loads((tmp_path / "investigations" / "inv-w-2.json").read_text())
        assert data["investigation_id"] == "inv-w-2"
        assert data["service"] == "checkout"
        nodes = data.get("nodes") or []
        assert len(nodes) == 1
        node = nodes[0]
        content = node["content"]
        assert content["root_cause"] == "checkout pool exhausted"
        assert content["confidence"] == 87
        assert "logs" in content["evidence_snapshot_keys"]
        assert content["remediation"] == {"immediate_action": "scale"}

    def test_index_entry_appended(self, tmp_path):
        investigation_store_runner(_ctx(investigation_id="inv-idx-1"))
        idx_path = tmp_path / "investigations" / "_index.jsonl"
        assert idx_path.exists()
        lines = [json.loads(ln) for ln in idx_path.read_text().splitlines() if ln.strip()]
        # At least one entry for this investigation
        assert any(r["investigation_id"] == "inv-idx-1" for r in lines)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_second_run_deduplicates_by_file_existence(self):
        first = investigation_store_runner(_ctx(investigation_id="inv-dup-1"))
        second = investigation_store_runner(_ctx(investigation_id="inv-dup-1"))
        assert first["status"] == "success"
        assert second["status"] == "deduplicated"
        assert second["deduplicated"] is True
        assert second["record_id"] == first["record_id"]

    def test_different_investigations_produce_distinct_files(self, tmp_path):
        investigation_store_runner(_ctx(investigation_id="inv-a"))
        investigation_store_runner(_ctx(investigation_id="inv-b"))
        assert (tmp_path / "investigations" / "inv-a.json").exists()
        assert (tmp_path / "investigations" / "inv-b.json").exists()


# ---------------------------------------------------------------------------
# Skipped non-actionable outcomes
# ---------------------------------------------------------------------------

class TestSkipNonActionable:
    @pytest.mark.parametrize("prefix", [
        "INSUFFICIENT EVIDENCE: unknown",
        "META_QUERY_NOT_INCIDENT",
        "BLOCKED: gate G2",
        "LOW CONFIDENCE: something",
    ])
    def test_skip_prefixes(self, prefix):
        out = investigation_store_runner(_ctx(result={
            "root_cause": prefix, "confidence": 10,
        }))
        assert out["status"] == "skipped"

    def test_empty_root_cause_skipped(self):
        out = investigation_store_runner(_ctx(result={"confidence": 40}))
        assert out["status"] == "skipped"


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off_never_runs_module(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        assert rt.is_enabled() is False
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        assert results == []

    def test_module_flag_off_skipped(self, monkeypatch):
        monkeypatch.delenv(INVESTIGATION_STORE_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        # investigation_store is one of the ModuleResults; it should be
        # status=skipped due to per-module flag off
        by_name = {r.name: r for r in results}
        assert "investigation_store" in by_name
        assert by_name["investigation_store"].status == "skipped"

    def test_both_flags_on_runs(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        monkeypatch.delenv(RESOLUTION_MEMORY_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        by_name = {r.name: r for r in results}
        assert by_name["investigation_store"].status == "success"


# ---------------------------------------------------------------------------
# Cross-reference: DecisionTrace ID
# ---------------------------------------------------------------------------

class TestDecisionTraceRef:
    def test_dt_trace_id_lifted_when_present(self, tmp_path):
        out = investigation_store_runner(_ctx(
            investigation_id="inv-dt-1",
            decision_trace_meta={
                "trace_id": "abcdef1234567890",
                "decision_type": "hypothesis_selection",
                "decision": "winner",
                "confidence": 0.87,
            },
        ))
        assert out["decision_trace_id"] == "abcdef1234567890"
        # And the graph node content carries the same ID
        data = json.loads((tmp_path / "investigations" / "inv-dt-1.json").read_text())
        node = data["nodes"][0]
        assert node["content"]["decision_trace_id"] == "abcdef1234567890"

    def test_dt_ref_empty_when_absent(self):
        out = investigation_store_runner(_ctx(decision_trace_meta={}))
        assert out["decision_trace_id"] == ""


# ---------------------------------------------------------------------------
# Cross-reference: ResolutionMemory ID (via dependency ordering)
# ---------------------------------------------------------------------------

class TestResolutionMemoryRef:
    def test_rm_id_populated_when_rm_wrote_first(self, monkeypatch, tmp_path):
        """When both modules run in the same stage and RM produces a record
        first, InvestigationStore's envelope should carry the RM record_id."""
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                _ctx(investigation_id="inv-xref-1"))
        by_name = {r.name: r for r in results}
        rm = by_name["resolution_memory"]
        istore = by_name["investigation_store"]
        assert rm.status == "success"
        assert istore.status == "success"
        assert istore.metadata["resolution_memory_id"] == rm.metadata["record_id"]

    def test_rm_ref_empty_when_rm_module_off(self, monkeypatch):
        """If RM module is off, no RM record exists — InvestigationStore's
        RM ref should be empty (not a failure)."""
        monkeypatch.delenv(RESOLUTION_MEMORY_FEATURE_FLAG, raising=False)
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        istore = next(r for r in results if r.name == "investigation_store")
        assert istore.status == "success"
        assert istore.metadata["resolution_memory_id"] == ""


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------

class TestDependencyOrdering:
    def test_rm_runs_before_is(self, monkeypatch):
        """Verifies runtime honors the declared dependency."""
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_PERSIST)
        names = [s.name for s in specs]
        assert names.index("resolution_memory") < names.index("investigation_store")


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_save_graph_exception_captured(self, monkeypatch):
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from intelligence import investigation_store as _is_mod
        with patch.object(_is_mod.InvestigationStore, "save_graph",
                           side_effect=RuntimeError("disk offline")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(investigation_id="inv-fail-1"))
        istore = next(r for r in results if r.name == "investigation_store")
        assert istore.status == "failed"
        assert istore.error_type == "RuntimeError"

    def test_failure_does_not_stop_rm_module(self, monkeypatch):
        """When InvestigationStore fails, ResolutionMemory (which runs
        BEFORE it via dependencies) is unaffected."""
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from intelligence import investigation_store as _is_mod
        with patch.object(_is_mod.InvestigationStore, "save_graph",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(investigation_id="inv-iso"))
        by_name = {r.name: r for r in results}
        assert by_name["resolution_memory"].status == "success"
        assert by_name["investigation_store"].status == "failed"


# ---------------------------------------------------------------------------
# Receipt metadata contract
# ---------------------------------------------------------------------------

class TestReceiptMetadata:
    def test_metadata_carries_required_fields(self, monkeypatch):
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                _ctx(investigation_id="inv-meta-1"))
        istore = next(r for r in results if r.name == "investigation_store")
        # ModuleResult fields the mission spec requires
        assert istore.name == "investigation_store"
        assert istore.status == "success"
        assert istore.elapsed_ms >= 0
        # Runner-supplied metadata
        for k in ("record_id", "deduplicated", "graph_path",
                  "decision_trace_id", "resolution_memory_id", "version"):
            assert k in istore.metadata

    def test_lifted_to_phase_receipt(self, monkeypatch):
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("persist") as _r:
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                    _ctx(investigation_id="inv-lift"))
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        receipt = col.to_list()[0]
        arr = receipt["metadata"]["intelligence"]
        names = [entry["name"] for entry in arr]
        assert "investigation_store" in names


# ---------------------------------------------------------------------------
# Replay compatibility
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_short_circuit_never_writes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.setenv(INVESTIGATION_STORE_FEATURE_FLAG, "true")
        from unittest.mock import MagicMock, Mock
        from supervisor.agent import SentinalAISupervisor
        from supervisor.replay import ReplayStore

        replay_dir = tmp_path / "replay"
        replay_dir.mkdir()
        store = ReplayStore(replay_dir=str(replay_dir))
        (replay_dir / "INC_IS_20260704T100000Z.json").write_text(json.dumps({
            "case_id": "INC_IS",
            "result": {"root_cause": "cached IS", "confidence": 91,
                        "evidence_timeline": [], "reasoning": "cached"},
            "evidence": {},
        }))

        supervisor = SentinalAISupervisor()
        supervisor._replay_store = store
        for name in supervisor.workers:
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(side_effect=lambda a, p: {})

        result = supervisor.investigate("INC_IS", replay=True)
        # No phase receipts on replay path
        assert "_phase_receipts" not in result
        # No graph file was written
        assert not (tmp_path / "investigations" / "inv-INC_IS.json").exists()


# ---------------------------------------------------------------------------
# STOP CONDITION verification — InvestigationStore + EvidenceGraph unchanged
# ---------------------------------------------------------------------------

class TestUpstreamUnchanged:
    def test_investigation_store_api_unchanged(self):
        from intelligence.investigation_store import InvestigationStore
        for name in ("save_graph", "load_graph", "commit_investigation",
                     "find_by_service", "find_by_incident_type", "list_recent"):
            assert hasattr(InvestigationStore, name), f"missing {name}"

    def test_evidence_graph_api_unchanged(self):
        from intelligence.evidence_graph import EvidenceGraph
        for name in ("add_node", "add_edge", "set_phase", "get_node",
                     "get_edge", "node_count", "edge_count",
                     "all_nodes", "all_edges", "to_dict", "from_dict"):
            assert hasattr(EvidenceGraph, name), f"missing {name}"
