"""Historical Intelligence Retrieval activation tests.

Verifies the first READ consumer of the persisted intelligence corpus:
``historical_lookup`` runs at POST_CLASSIFY and consults both
ResolutionMemoryStore and InvestigationStore for prior matches on the
current (service, incident_type). Never writes anything.

Coverage:
- ModuleSpec metadata (stage=POST_CLASSIFY, feature flag, priority)
- Skip when neither service nor incident_type is available
- Success with empty matches when stores are empty
- Success with populated matches from ResolutionMemory
- Success with populated matches from InvestigationStore
- Compact payload — root cause truncated, list bounded to _MAX
- Feature flag on/off + master flag off semantics
- Receipt metadata lift through the runtime
- Failure isolation — RM/IS query failure captured, other module unaffected
- Read-only guarantee — no rows added to either store
- Agent-wiring string checks (install_default_modules seam preserved)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.historical_lookup import (
    HISTORICAL_LOOKUP_FEATURE_FLAG,
    HISTORICAL_LOOKUP_SPEC,
    LOOKUP_VERSION,
    _MAX_MATCHES_PER_SOURCE,
    _ROOT_CAUSE_HEAD,
    historical_lookup_runner,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _FakeCres:
    incident_type: str = "saturation"


def _ctx(*, investigation_id="inv-INC1",
         service="checkout",
         incident_type="saturation",
         incident_id="INC1"):
    """Build a POST_CLASSIFY RuntimeContext."""
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_CLASSIFY,
        fetch_out={
            "incident": {"incident_id": incident_id, "summary": "x",
                          "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
    )


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path, monkeypatch):
    """Point OPS_DB_PATH + INVESTIGATIONS_DIR at fresh tmp paths per test."""
    db_path = tmp_path / "ops_intelligence.db"
    inv_dir = tmp_path / "investigations"
    inv_dir.mkdir()
    monkeypatch.setenv("OPS_DB_PATH", str(db_path))
    monkeypatch.setenv("OPS_DB_ENABLED", "true")
    monkeypatch.setenv("INVESTIGATIONS_DIR", str(inv_dir))
    # Reset the ops_persistence singleton so each test starts from a fresh init
    import database.ops_persistence as _ops
    monkeypatch.setattr(_ops, "_instance", None, raising=False)


# ---------------------------------------------------------------------------
# Seeders — use the underlying stores directly to arrange test data.
# The activation runners for RM/IS themselves are tested elsewhere.
# ---------------------------------------------------------------------------

def _seed_resolution_memory(
    *,
    investigation_id: str,
    service: str,
    incident_type: str,
    root_cause: str = "checkout DB pool exhausted",
    confidence: int = 78,
) -> str:
    """Seed ResolutionMemoryStore with a single record, return memory_id."""
    # Ensure schema exists at the env-var-current db_path
    from supervisor.intelligence_modules.resolution_memory import _ensure_schema
    _ensure_schema(os.environ["OPS_DB_PATH"])

    from intelligence.resolution_memory import (
        ResolutionMemory,
        ResolutionMemoryStore,
    )
    memory = ResolutionMemory.from_investigation(
        investigation_id=investigation_id,
        incident_id=investigation_id.replace("inv-", ""),
        service=service,
        incident_type=incident_type,
        result={"root_cause": root_cause, "confidence": confidence},
        evidence={},
    )
    ResolutionMemoryStore(os.environ["OPS_DB_PATH"]).record(memory)
    return memory.memory_id


def _seed_investigation(
    *,
    investigation_id: str,
    service: str,
    incident_type: str,
) -> None:
    """Seed InvestigationStore index with one investigation."""
    from intelligence.evidence_graph import EvidenceGraph
    from intelligence.investigation_store import InvestigationStore
    from intelligence.schema import InvestigationPhase
    graph = EvidenceGraph(
        investigation_id=investigation_id,
        incident_id=investigation_id.replace("inv-", ""),
        service=service,
        incident_type=incident_type,
        phase=InvestigationPhase.RESOLVED,
    )
    InvestigationStore(investigations_dir=os.environ["INVESTIGATIONS_DIR"]).save_graph(graph)


# ---------------------------------------------------------------------------
# ModuleSpec metadata
# ---------------------------------------------------------------------------

class TestSpec:
    def test_spec_name(self):
        assert HISTORICAL_LOOKUP_SPEC.name == "historical_lookup"

    def test_spec_stage_is_post_classify(self):
        assert HISTORICAL_LOOKUP_SPEC.stage == IntelligenceStage.POST_CLASSIFY

    def test_spec_feature_flag(self):
        assert HISTORICAL_LOOKUP_SPEC.feature_flag == HISTORICAL_LOOKUP_FEATURE_FLAG
        assert HISTORICAL_LOOKUP_FEATURE_FLAG == "ENABLE_HISTORICAL_LOOKUP"

    def test_spec_priority(self):
        assert HISTORICAL_LOOKUP_SPEC.priority == 100

    def test_spec_has_no_dependencies(self):
        assert HISTORICAL_LOOKUP_SPEC.dependencies == ()


# ---------------------------------------------------------------------------
# Skip semantics
# ---------------------------------------------------------------------------

class TestSkipSemantics:
    def test_no_service_no_incident_type_skips(self):
        ctx = RuntimeContext(
            investigation_id="inv-empty",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"incident": {"incident_id": "INC1"}, "service": ""},
            cres=_FakeCres(incident_type=""),
        )
        out = historical_lookup_runner(ctx)
        assert out["status"] == "skipped"
        assert out["reason"] == "no_service_and_no_incident_type"
        assert out["version"] == LOOKUP_VERSION

    def test_service_only_is_sufficient(self):
        ctx = RuntimeContext(
            investigation_id="inv-svc",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"service": "checkout"},
            cres=_FakeCres(incident_type=""),
        )
        out = historical_lookup_runner(ctx)
        assert out["status"] == "success"

    def test_incident_type_only_is_sufficient(self):
        ctx = RuntimeContext(
            investigation_id="inv-typ",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"service": ""},
            cres=_FakeCres(incident_type="saturation"),
        )
        out = historical_lookup_runner(ctx)
        assert out["status"] == "success"


# ---------------------------------------------------------------------------
# Empty stores yield empty matches (but success, not failure)
# ---------------------------------------------------------------------------

class TestEmptyStores:
    def test_empty_stores_return_success_with_zero_matches(self):
        out = historical_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["resolution_memory_matches"] == []
        assert out["investigation_matches"] == []
        assert out["match_counts"] == {"resolution_memory": 0, "investigation": 0}

    def test_shape_present_even_with_empty_matches(self):
        out = historical_lookup_runner(_ctx())
        assert out["service"] == "checkout"
        assert out["incident_type"] == "saturation"
        assert out["version"] == LOOKUP_VERSION


# ---------------------------------------------------------------------------
# ResolutionMemory matching
# ---------------------------------------------------------------------------

class TestResolutionMemoryMatching:
    def test_matches_by_service_and_incident_type(self):
        _seed_resolution_memory(
            investigation_id="inv-prior-A",
            service="checkout",
            incident_type="saturation",
            root_cause="DB pool exhausted at checkout",
            confidence=82,
        )
        out = historical_lookup_runner(_ctx())
        assert out["status"] == "success"
        rm = out["resolution_memory_matches"]
        assert len(rm) == 1
        m = rm[0]
        assert m["memory_id"]
        assert m["confidence"] == 82
        assert "DB pool" in m["root_cause_head"]

    def test_does_not_match_other_service(self):
        _seed_resolution_memory(
            investigation_id="inv-other",
            service="payments",
            incident_type="saturation",
        )
        out = historical_lookup_runner(_ctx(service="checkout"))
        assert out["resolution_memory_matches"] == []

    def test_multiple_matches_bounded_by_max(self):
        for i in range(_MAX_MATCHES_PER_SOURCE + 3):
            _seed_resolution_memory(
                investigation_id=f"inv-many-{i}",
                service="checkout",
                incident_type="saturation",
                root_cause=f"cause #{i}",
            )
        out = historical_lookup_runner(_ctx())
        assert len(out["resolution_memory_matches"]) == _MAX_MATCHES_PER_SOURCE
        assert out["match_counts"]["resolution_memory"] == _MAX_MATCHES_PER_SOURCE

    def test_root_cause_head_is_truncated(self):
        long_rc = "x" * (_ROOT_CAUSE_HEAD + 500)
        _seed_resolution_memory(
            investigation_id="inv-long",
            service="checkout",
            incident_type="saturation",
            root_cause=long_rc,
        )
        out = historical_lookup_runner(_ctx())
        head = out["resolution_memory_matches"][0]["root_cause_head"]
        assert len(head) <= _ROOT_CAUSE_HEAD


# ---------------------------------------------------------------------------
# InvestigationStore matching
# ---------------------------------------------------------------------------

class TestInvestigationStoreMatching:
    def test_matches_by_service(self):
        _seed_investigation(
            investigation_id="inv-past-1",
            service="checkout",
            incident_type="saturation",
        )
        out = historical_lookup_runner(_ctx())
        assert out["status"] == "success"
        inv = out["investigation_matches"]
        assert len(inv) == 1
        assert inv[0]["investigation_id"] == "inv-past-1"
        assert inv[0]["service"] == "checkout"

    def test_falls_back_to_incident_type_when_no_service_match(self):
        _seed_investigation(
            investigation_id="inv-typ-1",
            service="payments",  # different service
            incident_type="saturation",
        )
        out = historical_lookup_runner(_ctx(service="unknown-svc",
                                              incident_type="saturation"))
        inv = out["investigation_matches"]
        assert len(inv) == 1
        assert inv[0]["investigation_id"] == "inv-typ-1"

    def test_investigation_matches_bounded(self):
        for i in range(_MAX_MATCHES_PER_SOURCE + 4):
            _seed_investigation(
                investigation_id=f"inv-bnd-{i}",
                service="checkout",
                incident_type="saturation",
            )
        out = historical_lookup_runner(_ctx())
        assert len(out["investigation_matches"]) == _MAX_MATCHES_PER_SOURCE


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off_does_not_execute(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        assert rt.is_enabled() is False
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(HISTORICAL_LOOKUP_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        hl_result = next(r for r in results if r.name == "historical_lookup")
        assert hl_result.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        hl_result = next(r for r in results if r.name == "historical_lookup")
        assert hl_result.status == "success"
        assert hl_result.metadata["version"] == LOOKUP_VERSION


# ---------------------------------------------------------------------------
# Receipt metadata lift
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_module_result_metadata_lifted_to_receipt(self, monkeypatch):
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        from supervisor.phase_receipts import PhaseReceiptCollector
        _seed_resolution_memory(
            investigation_id="inv-recpt-src",
            service="checkout",
            incident_type="saturation",
        )
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("classify") as _r:
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY,
                                    _ctx(investigation_id="inv-recpt-lift"))
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        receipt = col.to_list()[0]
        arr = receipt["metadata"]["intelligence"]
        entry = next(e for e in arr if e["name"] == "historical_lookup")
        assert entry["status"] == "success"
        assert entry["metadata"]["match_counts"]["resolution_memory"] >= 1


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_rm_query_failure_returns_empty_matches_not_crash(self, monkeypatch):
        """A raise inside ResolutionMemoryStore.query() must NOT propagate.
        The source-specific helper swallows it and returns empty."""
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        from intelligence import resolution_memory as _rm
        with patch.object(_rm.ResolutionMemoryStore, "query",
                           side_effect=RuntimeError("db offline")):
            out = historical_lookup_runner(_ctx())
        # Runner still returns success — one source fails, other keeps working
        assert out["status"] == "success"
        assert out["resolution_memory_matches"] == []

    def test_is_query_failure_returns_empty_matches_not_crash(self, monkeypatch):
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        from intelligence import investigation_store as _is
        with patch.object(_is.InvestigationStore, "find_by_service",
                           side_effect=OSError("index missing")):
            out = historical_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["investigation_matches"] == []

    def test_runtime_catches_unexpected_exceptions(self, monkeypatch):
        """If the runner raises for some totally unexpected reason, the
        runtime's outer isolation catches it and marks failed."""
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        # Sabotage the extractor
        from supervisor.intelligence_modules import historical_lookup as _hl
        with patch.object(_hl, "_extract_service",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        hl_result = next(r for r in results if r.name == "historical_lookup")
        assert hl_result.status == "failed"
        assert hl_result.error_type == "ValueError"


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_lookup_does_not_write_resolution_memory(self, monkeypatch):
        """The lookup must not INSERT new rows into resolution_memories."""
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        # Seed one row so the store is initialized
        _seed_resolution_memory(
            investigation_id="inv-baseline",
            service="checkout",
            incident_type="saturation",
        )
        from intelligence.resolution_memory import ResolutionMemoryStore
        store = ResolutionMemoryStore(os.environ["OPS_DB_PATH"])
        before = len(store.query(limit=1000))
        historical_lookup_runner(_ctx())
        after = len(store.query(limit=1000))
        assert before == after

    def test_lookup_does_not_write_investigation_store(self):
        _seed_investigation(
            investigation_id="inv-baseline-2",
            service="checkout",
            incident_type="saturation",
        )
        from intelligence.investigation_store import InvestigationStore
        store = InvestigationStore(investigations_dir=os.environ["INVESTIGATIONS_DIR"])
        before = len(store.find_by_service("checkout"))
        historical_lookup_runner(_ctx())
        after = len(store.find_by_service("checkout"))
        assert before == after


# ---------------------------------------------------------------------------
# Dependency + ordering compatibility
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_post_classify_stage_isolated_from_post_persist(self, monkeypatch):
        """Running POST_CLASSIFY must not execute POST_PERSIST modules."""
        monkeypatch.setenv(HISTORICAL_LOOKUP_FEATURE_FLAG, "true")
        monkeypatch.setenv("ENABLE_RESOLUTION_MEMORY_WRITE", "true")
        monkeypatch.setenv("ENABLE_INVESTIGATION_STORE_WRITE", "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        names = {r.name for r in results}
        assert "historical_lookup" in names
        assert "resolution_memory" not in names
        assert "investigation_store" not in names


# ---------------------------------------------------------------------------
# Agent wiring — string-form checks on the registration seam
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_registered_via_install_default_modules(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("historical_lookup")

    def test_module_appears_in_post_classify_plan(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_CLASSIFY)
        assert any(s.name == "historical_lookup" for s in specs)

    def test_agent_source_unchanged_by_this_activation(self):
        """The activation must not require any change to agent.py — it plugs
        in via the install_default_modules seam that Phase 20/21 already added."""
        import supervisor.agent as m
        src = open(m.__file__).read()
        # These strings must still be present (Phase 19 seam)
        assert "from supervisor.intelligence_modules import install_default_modules" in src
        assert "install_default_modules(_intel)" in src
        # This module's name should NOT appear in agent.py (registration is
        # via __init__.py, not directly).
        assert "historical_lookup" not in src


# ---------------------------------------------------------------------------
# JSON-safety — the metadata dict must serialize cleanly
# ---------------------------------------------------------------------------

class TestJsonSafety:
    def test_payload_is_json_serializable(self):
        _seed_resolution_memory(
            investigation_id="inv-json-1",
            service="checkout",
            incident_type="saturation",
        )
        _seed_investigation(
            investigation_id="inv-json-2",
            service="checkout",
            incident_type="saturation",
        )
        out = historical_lookup_runner(_ctx())
        # Must round-trip through JSON without loss
        s = json.dumps(out)
        d = json.loads(s)
        assert d["status"] == "success"
        assert d["match_counts"]["resolution_memory"] == 1
        assert d["match_counts"]["investigation"] == 1
