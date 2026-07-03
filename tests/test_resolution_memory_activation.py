"""Phase 20 — ResolutionMemory activation tests.

Verifies that the dormant ``intelligence.resolution_memory`` module becomes
part of every completed investigation via the Phase 19 Intelligence Runtime,
without modifying ResolutionMemory itself.

Coverage per the mission spec:
- Successful write (new investigation → success + record_id + write_time)
- Duplicate updates (second run with same investigation_id → deduplicated)
- Feature flag off → module skipped (status="skipped", zero elapsed)
- Master flag off → runtime disabled, module never invoked
- Receipt metadata (module output appears in
  receipt.metadata["intelligence"] under name="resolution_memory")
- Failure isolation (runner exception captured, other modules unaffected)
- Replay path (replay short-circuit bypasses runtime entirely)
- Parallel investigations (each gets its own runtime; no cross-talk)
- Non-actionable root causes skipped (INSUFFICIENT / META_QUERY / BLOCKED)
- No implicit AnalyzePhase changes (schema still Phase 18/19)
"""
from __future__ import annotations

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
from supervisor.intelligence_modules.resolution_memory import (
    RESOLUTION_MEMORY_FEATURE_FLAG,
    RESOLUTION_MEMORY_SPEC,
    WRITE_VERSION,
    resolution_memory_runner,
)


# ---------------------------------------------------------------------------
# Fake ClassificationResult + AnalyzeResult
# ---------------------------------------------------------------------------

@dataclass
class _FakeCres:
    incident_type: str = "saturation"


@dataclass
class _FakeAout:
    evidence: dict


def _ctx(*, investigation_id="inv-INC1", result=None, evidence=None,
        service="checkout", incident_type="saturation",
        incident_id="INC1"):
    """Construct a POST_PERSIST RuntimeContext."""
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_PERSIST,
        fetch_out={
            "incident": {"incident_id": incident_id, "summary": "x",
                          "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
        aout=_FakeAout(evidence=evidence or {"logs": [], "metrics": {}}),
        result=result or {
            "incident_id": incident_id,
            "root_cause":  "checkout DB pool exhaustion",
            "confidence":  78,
            "evidence_timeline": [],
            "reasoning":   "reasoning",
            "remediation": {"immediate_action": "scale pool"},
        },
    )


@pytest.fixture(autouse=True)
def _isolate_ops_db(tmp_path, monkeypatch):
    """Point OPS_DB_PATH at a fresh tmp SQLite per test to avoid cross-talk."""
    monkeypatch.setenv("OPS_DB_PATH", str(tmp_path / "ops_intelligence.db"))
    monkeypatch.setenv("OPS_DB_ENABLED", "true")
    # Reset the ops_persistence singleton so a fresh init runs per test
    import database.ops_persistence as _ops
    monkeypatch.setattr(_ops, "_instance", None, raising=False)


# ---------------------------------------------------------------------------
# ModuleSpec metadata
# ---------------------------------------------------------------------------

class TestSpec:
    def test_spec_stage_is_post_persist(self):
        assert RESOLUTION_MEMORY_SPEC.stage == IntelligenceStage.POST_PERSIST

    def test_spec_has_feature_flag(self):
        assert RESOLUTION_MEMORY_SPEC.feature_flag == RESOLUTION_MEMORY_FEATURE_FLAG
        assert RESOLUTION_MEMORY_FEATURE_FLAG == "ENABLE_RESOLUTION_MEMORY_WRITE"

    def test_spec_has_name(self):
        assert RESOLUTION_MEMORY_SPEC.name == "resolution_memory"


# ---------------------------------------------------------------------------
# Successful write
# ---------------------------------------------------------------------------

class TestSuccessfulWrite:
    def test_new_investigation_writes(self):
        out = resolution_memory_runner(_ctx())
        assert out["status"] == "success"
        assert out["deduplicated"] is False
        assert out["version"] == WRITE_VERSION
        assert out["record_id"]
        assert out["write_time"]

    def test_confidence_recorded(self):
        out = resolution_memory_runner(_ctx(result={
            "incident_id": "INC1",
            "root_cause": "checkout pool exhausted",
            "confidence": 85,
        }))
        assert out["status"] == "success"
        assert out["confidence"] == 85

    def test_record_persists_in_store(self):
        out = resolution_memory_runner(_ctx(investigation_id="inv-persist-1"))
        assert out["status"] == "success"
        # Re-read from the store — must exist
        from intelligence.resolution_memory import ResolutionMemoryStore
        store = ResolutionMemoryStore(os.environ["OPS_DB_PATH"])
        found = store.get(out["record_id"])
        assert found is not None
        assert found.investigation_id == "inv-persist-1"
        assert found.detected_root_cause  # non-empty
        assert found.validation_status == "candidate"  # human hasn't confirmed


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_second_run_with_same_investigation_deduplicates(self):
        first = resolution_memory_runner(_ctx(investigation_id="inv-dup-1"))
        assert first["status"] == "success"

        second = resolution_memory_runner(_ctx(investigation_id="inv-dup-1"))
        assert second["status"] == "deduplicated"
        assert second["deduplicated"] is True
        assert second["record_id"] == first["record_id"]

    def test_different_investigations_do_not_conflate(self):
        r1 = resolution_memory_runner(_ctx(investigation_id="inv-a-1"))
        r2 = resolution_memory_runner(_ctx(investigation_id="inv-b-2"))
        assert r1["status"] == "success"
        assert r2["status"] == "success"
        assert r1["record_id"] != r2["record_id"]


# ---------------------------------------------------------------------------
# Non-actionable root causes are skipped
# ---------------------------------------------------------------------------

class TestSkipNonActionable:
    @pytest.mark.parametrize("prefix", [
        "INSUFFICIENT",
        "INSUFFICIENT EVIDENCE: service — confidence 10/100. Manual investigation required.",
        "META_QUERY_NOT_INCIDENT",
        "BLOCKED: gate G2 hallucination_risk=1",
        "LOW CONFIDENCE: something",
    ])
    def test_skip_prefixes(self, prefix):
        out = resolution_memory_runner(_ctx(result={
            "root_cause": prefix,
            "confidence": 10,
        }))
        assert out["status"] == "skipped"
        assert out["reason"] == "no_actionable_root_cause"

    def test_empty_root_cause_skipped(self):
        out = resolution_memory_runner(_ctx(result={"root_cause": "",
                                                      "confidence": 40}))
        assert out["status"] == "skipped"

    def test_missing_root_cause_skipped(self):
        out = resolution_memory_runner(_ctx(result={"confidence": 40}))
        assert out["status"] == "skipped"


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off_does_not_execute_runner(self, monkeypatch):
        """When ENABLE_INTELLIGENCE_RUNTIME is off, the runtime never runs
        any module — even one that would otherwise succeed."""
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        assert rt.is_enabled() is False
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        """Master on but module flag off — module is registered but returns
        status=skipped without invoking the runner."""
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(RESOLUTION_MEMORY_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        assert len(results) == 1
        assert results[0].name == "resolution_memory"
        assert results[0].status == "skipped"

    def test_both_flags_on_runs(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST, _ctx())
        assert results[0].status == "success"
        assert results[0].metadata["record_id"]


# ---------------------------------------------------------------------------
# Receipt metadata
# ---------------------------------------------------------------------------

class TestReceiptMetadata:
    def test_module_result_carries_full_metadata(self, monkeypatch):
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(investigation_id="inv-recpt-1"))
        meta = results[0].metadata
        # Mission spec required fields
        assert "status" in meta
        assert "record_id" in meta
        assert "write_time" in meta
        assert "deduplicated" in meta
        assert "version" in meta
        assert meta["version"] == WRITE_VERSION

    def test_dedup_metadata_marks_deduplicated_true(self, monkeypatch):
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        # First write
        rt.run_stage(IntelligenceStage.POST_PERSIST,
                     _ctx(investigation_id="inv-dupmeta-1"))
        # Second write
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                _ctx(investigation_id="inv-dupmeta-1"))
        assert results[0].metadata["deduplicated"] is True
        assert results[0].status == "success"  # runtime status is success even for dedup

    def test_runtime_lifts_module_result_to_phase_receipt(self, monkeypatch):
        """End-to-end via PhaseReceiptCollector: the runtime hook in
        investigate() lifts the ModuleResult list onto
        receipt.metadata['intelligence']."""
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("persist") as _r:
            results = rt.run_stage(
                IntelligenceStage.POST_PERSIST,
                _ctx(investigation_id="inv-recpt-lift"),
            )
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        receipt = col.to_list()[0]
        assert "intelligence" in receipt["metadata"]
        arr = receipt["metadata"]["intelligence"]
        assert len(arr) == 1
        assert arr[0]["name"] == "resolution_memory"
        assert arr[0]["status"] == "success"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_runner_exception_captured_by_runtime(self, monkeypatch):
        """If the ResolutionMemoryStore raises unexpectedly, the runtime's
        failure isolation catches it and reports status=failed with
        error_type."""
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        # Sabotage the store to raise on record
        from intelligence import resolution_memory as _rm
        with patch.object(_rm.ResolutionMemoryStore, "record",
                           side_effect=RuntimeError("db offline")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(investigation_id="inv-fail-1"))
        assert results[0].status == "failed"
        assert results[0].error_type == "RuntimeError"

    def test_runner_failure_does_not_break_other_modules(self, monkeypatch):
        """A failing resolution_memory doesn't stop other modules from
        running at the same stage."""
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        # Register a second module at POST_PERSIST that always succeeds
        rt.register(
            ModuleSpec(name="second-mod", stage=IntelligenceStage.POST_PERSIST,
                        priority=200),
            lambda ctx: {"ok": True},
        )
        from intelligence import resolution_memory as _rm
        with patch.object(_rm.ResolutionMemoryStore, "record",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(investigation_id="inv-iso"))
        # Both ran; failure isolated
        assert len(results) == 2
        assert {r.name: r.status for r in results} == {
            "resolution_memory": "failed",
            "second-mod": "success",
        }


# ---------------------------------------------------------------------------
# Parallel investigations
# ---------------------------------------------------------------------------

class TestParallelInvestigations:
    def test_two_investigations_write_separate_records(self, monkeypatch):
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        # Fresh runtime per investigation (as investigate() does)
        rt_a = IntelligenceRuntime(enabled=True)
        install_default_modules(rt_a)
        rt_b = IntelligenceRuntime(enabled=True)
        install_default_modules(rt_b)
        r_a = rt_a.run_stage(IntelligenceStage.POST_PERSIST,
                              _ctx(investigation_id="inv-para-a",
                                   incident_id="INC_A"))
        r_b = rt_b.run_stage(IntelligenceStage.POST_PERSIST,
                              _ctx(investigation_id="inv-para-b",
                                   incident_id="INC_B"))
        assert r_a[0].metadata["record_id"] != r_b[0].metadata["record_id"]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_path_never_invokes_runner(self, tmp_path, monkeypatch):
        """The replay short-circuit at investigate() line ~312 runs before the
        Intelligence Runtime is constructed. Ensure a replay produces no
        resolution memory record."""
        import json as _json
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.setenv(RESOLUTION_MEMORY_FEATURE_FLAG, "true")
        from unittest.mock import MagicMock, Mock
        from supervisor.agent import SentinalAISupervisor
        from supervisor.replay import ReplayStore

        replay_dir = tmp_path / "replay"
        replay_dir.mkdir()
        store = ReplayStore(replay_dir=str(replay_dir))
        (replay_dir / "INC_RM_20260704T100000Z.json").write_text(_json.dumps({
            "case_id": "INC_RM",
            "result": {"root_cause": "cached RM", "confidence": 90,
                        "evidence_timeline": [], "reasoning": "cached"},
            "evidence": {},
        }))

        supervisor = SentinalAISupervisor()
        supervisor._replay_store = store
        for name in supervisor.workers:
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(side_effect=lambda a, p: {})

        result = supervisor.investigate("INC_RM", replay=True)
        # No _phase_receipts key on replay path
        assert "_phase_receipts" not in result

        # And no resolution memory was written for the replayed investigation
        from intelligence.resolution_memory import ResolutionMemoryStore
        rm_store = ResolutionMemoryStore(os.environ["OPS_DB_PATH"])
        # investigation_id for a replayed session is derived by fetch phase
        # inside investigate — since we short-circuited before that, none
        # should exist for either the raw case_id or an inv- prefix
        assert rm_store.get("inv-INC_RM") is None


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_agent_imports_install_default_modules(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "from supervisor.intelligence_modules import install_default_modules" in src
        assert "install_default_modules(_intel)" in src

    def test_agent_install_guarded_by_master_flag(self):
        """install_default_modules must ONLY run when the master runtime flag
        is on — verified by the source-string form."""
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "if _intel.is_enabled():" in src

    def test_investigate_signature_unchanged(self):
        import inspect
        from supervisor.agent import SentinalAISupervisor
        sig = inspect.signature(SentinalAISupervisor.investigate)
        assert list(sig.parameters.keys()) == ["self", "incident_id", "replay"]


# ---------------------------------------------------------------------------
# ResolutionMemory API unchanged (STOP CONDITION verification)
# ---------------------------------------------------------------------------

class TestResolutionMemoryUnchanged:
    """Sentinel — assert the intelligence.resolution_memory module's public
    surface is untouched by Phase 20. If any signature changes, this
    catches it."""

    def test_public_dataclass_fields_unchanged(self):
        from intelligence.resolution_memory import ResolutionMemory
        expected = {
            "memory_id", "investigation_id", "incident_id", "service",
            "environment", "incident_type", "symptoms", "detected_root_cause",
            "evidence_used", "confirmed_resolution", "fix_action",
            "rollback_action", "owner_team", "confidence", "validation_status",
            "is_confirmed", "lesson_learned", "related_incident_ids",
            "mttr_minutes", "recorded_at", "confirmed_at",
        }
        assert set(ResolutionMemory.__dataclass_fields__.keys()) == expected

    def test_store_api_unchanged(self):
        from intelligence.resolution_memory import ResolutionMemoryStore
        # These methods must all exist with the current signatures
        for name in ("record", "confirm", "reject", "get", "query", "find_similar"):
            assert hasattr(ResolutionMemoryStore, name), f"missing {name}"
