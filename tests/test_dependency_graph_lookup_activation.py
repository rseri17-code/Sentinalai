"""DependencyGraphLookup activation tests."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    RuntimeContext,
)
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.dependency_graph_lookup import (
    DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG,
    DEPENDENCY_GRAPH_LOOKUP_SPEC,
    LOOKUP_VERSION,
    _MAX_EDGES_PER_DIRECTION,
    dependency_graph_lookup_runner,
)


@dataclass
class _FakeCres:
    incident_type: str = "saturation"


def _ctx(*, service="checkout", incident_type="saturation",
         incident_id="INC1", investigation_id="inv-INC1"):
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_CLASSIFY,
        fetch_out={
            "incident": {"incident_id": incident_id, "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
    )


_DDL = """
CREATE TABLE IF NOT EXISTS service_dependencies (
    dep_id         TEXT PRIMARY KEY,
    source_service TEXT NOT NULL DEFAULT '',
    target_service TEXT NOT NULL DEFAULT '',
    dep_type       TEXT NOT NULL DEFAULT 'runtime',
    strength       REAL NOT NULL DEFAULT 0.1,
    observed_count INTEGER NOT NULL DEFAULT 1,
    first_seen     TEXT NOT NULL DEFAULT '',
    last_seen      TEXT NOT NULL DEFAULT ''
);
"""


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ops_intelligence.db"
    monkeypatch.setenv("OPS_DB_PATH", str(db_path))
    monkeypatch.setenv("OPS_DB_ENABLED", "true")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    import database.ops_persistence as _ops
    monkeypatch.setattr(_ops, "_instance", None, raising=False)


def _seed_dep(
    *,
    source: str,
    target: str,
    dep_type: str = "runtime",
    strength: float = 0.5,
    observed: int = 3,
) -> None:
    import hashlib
    dep_id = hashlib.sha256(f"{source}:{target}:{dep_type}".encode()).hexdigest()[:16]
    conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
    conn.execute(
        "INSERT INTO service_dependencies "
        "(dep_id, source_service, target_service, dep_type, strength, "
        " observed_count, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
        (dep_id, source, target, dep_type, strength, observed,
         "2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()


class TestSpec:
    def test_name(self):
        assert DEPENDENCY_GRAPH_LOOKUP_SPEC.name == "dependency_graph_lookup"

    def test_stage(self):
        assert DEPENDENCY_GRAPH_LOOKUP_SPEC.stage == IntelligenceStage.POST_CLASSIFY

    def test_flag(self):
        assert DEPENDENCY_GRAPH_LOOKUP_SPEC.feature_flag == DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG
        assert DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG == "ENABLE_DEPENDENCY_GRAPH_LOOKUP"

    def test_priority(self):
        assert DEPENDENCY_GRAPH_LOOKUP_SPEC.priority == 400


class TestSkip:
    def test_no_service_skips(self):
        ctx = RuntimeContext(
            investigation_id="inv-none",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"service": ""},
            cres=_FakeCres(incident_type="x"),
        )
        out = dependency_graph_lookup_runner(ctx)
        assert out["status"] == "skipped"
        assert out["reason"] == "no_service"


class TestEmpty:
    def test_empty_store_returns_success_zero_edges(self):
        out = dependency_graph_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["upstream"] == []
        assert out["downstream"] == []
        assert out["affected_services"] == []
        assert out["edge_counts"] == {"upstream": 0, "downstream": 0, "affected": 0}


class TestTopology:
    def test_upstream_populated(self):
        # checkout depends on payments — so from checkout's perspective, payments is UPSTREAM.
        _seed_dep(source="checkout", target="payments", strength=0.8)
        out = dependency_graph_lookup_runner(_ctx(service="checkout"))
        up = out["upstream"]
        assert len(up) == 1
        assert up[0]["target_service"] == "payments"
        assert up[0]["strength"] == 0.8

    def test_downstream_populated(self):
        # ui-web depends on checkout — from checkout's perspective, ui-web is DOWNSTREAM.
        _seed_dep(source="ui-web", target="checkout", strength=0.6)
        out = dependency_graph_lookup_runner(_ctx(service="checkout"))
        down = out["downstream"]
        assert len(down) == 1
        assert down[0]["source_service"] == "ui-web"

    def test_affected_services_are_ranked_by_strength(self):
        _seed_dep(source="weak", target="checkout", strength=0.2)
        _seed_dep(source="strong", target="checkout", strength=0.9)
        out = dependency_graph_lookup_runner(_ctx(service="checkout"))
        affected = out["affected_services"]
        assert affected == ["strong", "weak"]

    def test_bounded_by_max(self):
        for i in range(_MAX_EDGES_PER_DIRECTION + 5):
            _seed_dep(source="checkout", target=f"tgt-{i:02d}",
                       strength=1.0 - i * 0.01)
            _seed_dep(source=f"src-{i:02d}", target="checkout",
                       strength=1.0 - i * 0.01)
        out = dependency_graph_lookup_runner(_ctx(service="checkout"))
        assert len(out["upstream"]) == _MAX_EDGES_PER_DIRECTION
        assert len(out["downstream"]) == _MAX_EDGES_PER_DIRECTION


class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        assert results == []

    def test_module_flag_off(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "dependency_graph_lookup")
        assert entry.status == "skipped"

    def test_module_flag_on(self, monkeypatch):
        monkeypatch.setenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "dependency_graph_lookup")
        assert entry.status == "success"
        assert entry.metadata["version"] == LOOKUP_VERSION


class TestReceiptLift:
    def test_metadata_flows_to_receipt(self, monkeypatch):
        monkeypatch.setenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        _seed_dep(source="checkout", target="db", strength=0.7)
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("classify") as _r:
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        entry = next(e for e in col.to_list()[0]["metadata"]["intelligence"]
                       if e["name"] == "dependency_graph_lookup")
        assert entry["status"] == "success"
        assert entry["metadata"]["edge_counts"]["upstream"] == 1


class TestFailureIsolation:
    def test_upstream_query_fails_downstream_still_works(self, monkeypatch):
        monkeypatch.setenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        _seed_dep(source="ui", target="checkout", strength=0.6)
        from intelligence import dependency_graph as _dg

        original = _dg.DependencyGraphStore.get_upstream

        def broken(self, service):
            raise RuntimeError("boom")

        with patch.object(_dg.DependencyGraphStore, "get_upstream", broken):
            out = dependency_graph_lookup_runner(_ctx(service="checkout"))
        assert out["status"] == "success"
        assert out["upstream"] == []
        assert len(out["downstream"]) == 1

    def test_runtime_catches_unexpected(self, monkeypatch):
        monkeypatch.setenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.intelligence_modules import dependency_graph_lookup as _dgl
        with patch.object(_dgl, "_extract_service",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "dependency_graph_lookup")
        assert entry.status == "failed"
        assert entry.error_type == "ValueError"


class TestReadOnly:
    def test_no_writes(self, monkeypatch):
        monkeypatch.setenv(DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        _seed_dep(source="a", target="checkout", strength=0.4)
        conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
        before = conn.execute("SELECT COUNT(*) FROM service_dependencies").fetchone()[0]
        conn.close()
        dependency_graph_lookup_runner(_ctx())
        conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
        after = conn.execute("SELECT COUNT(*) FROM service_dependencies").fetchone()[0]
        conn.close()
        assert before == after


class TestAgentWiring:
    def test_registered(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("dependency_graph_lookup")

    def test_agent_source_untouched(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "install_default_modules(_intel)" in src
        assert "dependency_graph_lookup" not in src


class TestJsonSafety:
    def test_payload_json_roundtrip(self):
        _seed_dep(source="checkout", target="db", strength=0.7)
        _seed_dep(source="ui", target="checkout", strength=0.6)
        out = dependency_graph_lookup_runner(_ctx(service="checkout"))
        s = json.dumps(out)
        d = json.loads(s)
        assert d["status"] == "success"
        assert d["edge_counts"]["upstream"] == 1
        assert d["edge_counts"]["downstream"] == 1
