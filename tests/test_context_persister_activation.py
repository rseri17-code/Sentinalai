"""IntelligenceContextPersister activation tests.

Verifies the first WRITE consumer of the intelligence read fan-out:
``intelligence_context_persister`` runs at POST_PERSIST, reads the
finalized phase receipts from ``ctx.phase_receipts``, builds an
IntelligenceContext, and writes a canonical JSON artifact to
``{INVESTIGATIONS_DIR}/{investigation_id}_intelligence.json``.
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
from supervisor.intelligence_modules.context_persister import (
    ARTIFACT_VERSION,
    INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG,
    INTELLIGENCE_CONTEXT_PERSIST_SPEC,
    intelligence_context_persister_runner,
)


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeCres:
    incident_type: str = "saturation"


def _intel_receipt(**intelligence_entries) -> dict:
    """Build one phase receipt containing intelligence entries."""
    return {
        "phase_name": "classify",
        "metadata": {
            "intelligence": [
                {"name": name, "status": "success",
                  "metadata": payload, "warnings": []}
                for name, payload in intelligence_entries.items()
            ],
        },
    }


def _base_result() -> dict:
    return {
        "incident_id": "INC1",
        "root_cause":  "checkout DB pool exhausted",
        "confidence":  78,
        "reasoning":   "reasoning",
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
        result=result or _base_result(),
        phase_receipts=tuple(phase_receipts),
    )


@pytest.fixture(autouse=True)
def _isolate_dirs(tmp_path, monkeypatch):
    d = tmp_path / "investigations"
    d.mkdir()
    monkeypatch.setenv("INVESTIGATIONS_DIR", str(d))
    yield d


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

class TestSpec:
    def test_name(self):
        assert INTELLIGENCE_CONTEXT_PERSIST_SPEC.name == "intelligence_context_persister"

    def test_stage(self):
        assert INTELLIGENCE_CONTEXT_PERSIST_SPEC.stage == IntelligenceStage.POST_PERSIST

    def test_flag(self):
        assert INTELLIGENCE_CONTEXT_PERSIST_SPEC.feature_flag == INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG
        assert INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG == "ENABLE_INTELLIGENCE_CONTEXT_PERSIST"

    def test_priority_after_rm_and_is(self):
        assert INTELLIGENCE_CONTEXT_PERSIST_SPEC.priority == 800


# ---------------------------------------------------------------------------
# Skip + empty
# ---------------------------------------------------------------------------

class TestSkip:
    def test_no_phase_receipts_skips(self):
        out = intelligence_context_persister_runner(_ctx(phase_receipts=()))
        assert out["status"] == "skipped"
        assert out["reason"] == "no_phase_receipts"
        assert out["version"] == ARTIFACT_VERSION


class TestEmpty:
    def test_receipts_without_intelligence_metadata_still_write(self, _isolate_dirs):
        """A receipt list that has no intelligence entries still produces
        an artifact (empty summary + investigation identity)."""
        receipts = [{"phase_name": "fetch", "metadata": {}}]
        out = intelligence_context_persister_runner(_ctx(phase_receipts=receipts,
                                                          investigation_id="inv-empty"))
        assert out["status"] == "success"
        assert out["module_names_present"] == []
        artifact = json.loads(open(out["artifact_path"]).read())
        assert artifact["investigation_id"] == "inv-empty"
        assert artifact["intelligence_modules_present"] == []


# ---------------------------------------------------------------------------
# Full artifact
# ---------------------------------------------------------------------------

class TestArtifactContract:
    def test_writes_artifact_at_expected_path(self, _isolate_dirs):
        r = _intel_receipt(historical_lookup={
            "service": "checkout", "incident_type": "saturation",
            "resolution_memory_matches": [{"memory_id": "m1", "root_cause_head": "cause",
                                            "confidence": 82, "recorded_at": "x"}],
            "investigation_matches": [],
        })
        out = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-full-1"))
        assert out["status"] == "success"
        expected = _isolate_dirs / "inv-full-1_intelligence.json"
        assert os.path.exists(expected)
        assert out["artifact_path"] == str(expected)

    def test_artifact_shape_matches_contract(self, _isolate_dirs):
        r = _intel_receipt(
            historical_lookup={
                "service": "checkout", "incident_type": "saturation",
                "resolution_memory_matches": [
                    {"memory_id": "m", "root_cause_head": "cause",
                      "confidence": 85, "recorded_at": "2026-07"},
                ],
                "investigation_matches": [
                    {"investigation_id": "i1", "created_at": "2026-06"},
                ],
            },
            pattern_recognition={
                "service": "checkout", "incident_type": "saturation",
                "pattern_matches": [
                    {"pattern_id": "p", "incident_type": "saturation",
                      "services": ["checkout"], "occurrence_count": 3,
                      "success_count": 2, "success_rate": 0.66,
                      "last_seen": "2026-07"},
                ],
            },
            incident_graph_lookup={
                "service": "checkout",
                "related_incident_ids": ["INC_OLD1", "INC_OLD2"],
            },
            dependency_graph_lookup={
                "service": "checkout",
                "upstream": [{"source_service": "checkout", "target_service": "db",
                                "dep_type": "runtime", "strength": 0.8}],
                "downstream": [],
                "affected_services": ["ui", "cart"],
            },
            episodic_memory_lookup={
                "service": "checkout", "incident_type": "saturation",
                "episodes": [{"episode_id": "e", "incident_id": "INC1",
                                "service": "checkout", "incident_type": "saturation",
                                "root_cause_head": "cause",
                                "resolution_action_head": "action",
                                "outcome": "resolved", "confidence": 0.9,
                                "recorded_at": "2026-07"}],
            },
            causal_graph_lookup={
                "service": "checkout",
                "severity": "high",
                "total_affected": 3,
                "affected": [{"service_id": "ui", "probability": 0.7,
                                "propagation_ms": 100, "path": ["checkout", "ui"]}],
            },
        )
        out = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-shape"))
        artifact = json.loads(open(out["artifact_path"]).read())

        # Required top-level keys
        for k in ("schema_version", "generated_at", "investigation_id",
                   "incident_id", "service", "incident_type",
                   "intelligence_modules_present",
                   "historical_matches_summary", "pattern_summary",
                   "graph_summary", "causal_summary", "episodic_summary",
                   "confidence_signals", "warnings", "source_phase_names"):
            assert k in artifact, f"missing key {k}"

        assert artifact["schema_version"] == ARTIFACT_VERSION
        assert artifact["investigation_id"] == "inv-shape"
        assert artifact["incident_id"] == "INC1"
        assert artifact["service"] == "checkout"
        assert artifact["incident_type"] == "saturation"
        assert len(artifact["intelligence_modules_present"]) == 6

        assert artifact["historical_matches_summary"]["resolution_memory_matches"][0]["memory_id"] == "m"
        assert artifact["pattern_summary"][0]["pattern_id"] == "p"
        assert set(artifact["graph_summary"]["related_incident_ids"]) == {"INC_OLD1", "INC_OLD2"}
        assert artifact["causal_summary"]["severity"] == "high"
        assert artifact["episodic_summary"][0]["episode_id"] == "e"
        assert artifact["confidence_signals"]["top_resolution_memory_confidence"] == 85
        assert artifact["confidence_signals"]["blast_radius_severity"] == "high"
        assert artifact["confidence_signals"]["has_recurring_pattern"] is True

    def test_artifact_is_json_safe_and_deterministic(self, _isolate_dirs):
        """Same inputs → byte-identical artifact bodies (up to generated_at)."""
        r = _intel_receipt(pattern_recognition={
            "service": "checkout", "incident_type": "saturation",
            "pattern_matches": [{"pattern_id": "p", "incident_type": "saturation",
                                  "occurrence_count": 2, "success_count": 1,
                                  "success_rate": 0.5, "last_seen": ""}],
        })
        out1 = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-det-1"))
        out2 = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-det-2"))
        a1 = json.loads(open(out1["artifact_path"]).read())
        a2 = json.loads(open(out2["artifact_path"]).read())
        # generated_at differs; investigation_id differs; everything else identical
        for k in a1.keys():
            if k in ("generated_at", "investigation_id"):
                continue
            assert a1[k] == a2[k], f"differ on {k}"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDedup:
    def test_second_run_deduplicates(self, _isolate_dirs):
        r = _intel_receipt(historical_lookup={
            "service": "checkout", "incident_type": "saturation",
            "resolution_memory_matches": [], "investigation_matches": [],
        })
        first = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-dup"))
        assert first["status"] == "success"
        second = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-dup"))
        assert second["status"] == "deduplicated"
        assert second["artifact_path"] == first["artifact_path"]


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt()]))
        entry = next(r for r in results if r.name == "intelligence_context_persister")
        assert entry.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                 _ctx(phase_receipts=[_intel_receipt(
                                     pattern_recognition={"service": "checkout",
                                                             "incident_type": "saturation",
                                                             "pattern_matches": []},
                                 )]))
        entry = next(r for r in results if r.name == "intelligence_context_persister")
        assert entry.status == "success"


# ---------------------------------------------------------------------------
# Robustness — malformed intelligence entries
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_malformed_intelligence_entries_do_not_crash(self, _isolate_dirs):
        """A receipt whose intelligence metadata contains nonsense should
        NOT crash the persister. Bad entries are silently isolated."""
        weird = {
            "phase_name": "classify",
            "metadata": {
                "intelligence": [
                    "not-a-dict",
                    {"name": "some_module", "metadata": "also-bad"},  # metadata not dict
                    None,
                    {"name": "pattern_recognition", "metadata":
                        {"pattern_matches": [{"pattern_id": "ok",
                                                "incident_type": "x",
                                                "occurrence_count": 1,
                                                "success_count": 0,
                                                "success_rate": 0.0,
                                                "last_seen": ""}]}},
                ],
            },
        }
        out = intelligence_context_persister_runner(
            _ctx(phase_receipts=[weird], investigation_id="inv-mal"))
        assert out["status"] == "success"
        artifact = json.loads(open(out["artifact_path"]).read())
        # The one good pattern survived
        assert len(artifact["pattern_summary"]) == 1

    def test_missing_intelligence_key_writes_empty_artifact(self, _isolate_dirs):
        receipts = [{"phase_name": "fetch", "metadata": {"other": "key"}}]
        out = intelligence_context_persister_runner(
            _ctx(phase_receipts=receipts, investigation_id="inv-noent"))
        assert out["status"] == "success"

    def test_warnings_are_aggregated_and_capped(self, _isolate_dirs):
        # Build a receipt with many warnings
        r = {
            "phase_name": "classify",
            "metadata": {
                "intelligence": [
                    {"name": "pattern_recognition",
                      "warnings": [f"w-{i}" for i in range(50)],
                      "metadata": {"service": "checkout"}},
                ],
            },
        }
        out = intelligence_context_persister_runner(
            _ctx(phase_receipts=[r], investigation_id="inv-warn"))
        artifact = json.loads(open(out["artifact_path"]).read())
        assert 0 < len(artifact["warnings"]) <= 20


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_io_error_captured_by_runtime(self, monkeypatch):
        monkeypatch.setenv(INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        # Sabotage json.dump to raise
        from supervisor.intelligence_modules import context_persister as _cp
        with patch.object(_cp.json, "dump", side_effect=OSError("disk full")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(phase_receipts=[_intel_receipt(
                                         pattern_recognition={"service": "checkout",
                                                                 "incident_type": "x",
                                                                 "pattern_matches": []},
                                     )]))
        entry = next(r for r in results if r.name == "intelligence_context_persister")
        assert entry.status == "failed"

    def test_failure_does_not_break_other_persist_modules(self, monkeypatch, tmp_path):
        monkeypatch.setenv(INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.intelligence_modules import context_persister as _cp
        with patch.object(_cp.json, "dump", side_effect=RuntimeError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_PERSIST,
                                     _ctx(phase_receipts=[_intel_receipt(
                                         pattern_recognition={"service": "checkout",
                                                                 "incident_type": "x",
                                                                 "pattern_matches": []},
                                     )]))
        by_name = {r.name: r.status for r in results}
        assert by_name["intelligence_context_persister"] == "failed"
        # Runtime kept going — other modules should have entries too
        assert "resolution_memory" in by_name  # even if skipped (flag off)


# ---------------------------------------------------------------------------
# RuntimeContext compatibility — the new phase_receipts field
# ---------------------------------------------------------------------------

class TestRuntimeContextBackCompat:
    def test_default_phase_receipts_is_empty(self):
        c = RuntimeContext(investigation_id="x",
                            stage=IntelligenceStage.POST_PERSIST)
        assert c.phase_receipts == ()

    def test_existing_field_shape_unchanged(self):
        # All old fields still constructible via kwargs
        c = RuntimeContext(
            investigation_id="x", stage=IntelligenceStage.POST_PERSIST,
            fetch_out={}, cres=None, cout=None, aout=None, result={},
        )
        assert c.result == {}
        assert c.phase_receipts == ()


# ---------------------------------------------------------------------------
# Agent hook wiring
# ---------------------------------------------------------------------------

class TestAgentHook:
    def test_agent_source_wires_phase_receipts_at_post_persist(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        # The intel_hook should conditionally set phase_receipts when stage
        # is POST_PERSIST
        assert "POST_PERSIST" in src
        assert "phase_receipts" in src
        assert "_phase_receipts.to_list()" in src


# ---------------------------------------------------------------------------
# Ordering — runs AFTER resolution_memory + investigation_store
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_registered_via_install_default_modules(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("intelligence_context_persister")

    def test_module_appears_in_post_persist_plan(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_PERSIST)
        names = [s.name for s in specs]
        assert names.index("intelligence_context_persister") > names.index("investigation_store")
        assert names.index("investigation_store") > names.index("resolution_memory")
