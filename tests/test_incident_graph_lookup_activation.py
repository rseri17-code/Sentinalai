"""IncidentGraphLookup activation tests.

Verifies the third READ consumer of the persisted intelligence corpus:
``incident_graph_lookup`` runs at POST_CLASSIFY and consults
``IncidentGraphStore.find_related_incidents(service)`` for other
incidents that touched the current service. Read-only.
"""
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
from supervisor.intelligence_modules.incident_graph_lookup import (
    INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG,
    INCIDENT_GRAPH_LOOKUP_SPEC,
    LOOKUP_VERSION,
    _MAX_RELATED_INCIDENTS,
    incident_graph_lookup_runner,
)


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeCres:
    incident_type: str = "saturation"


def _ctx(*, service="checkout", incident_type="saturation",
         incident_id="INC1", investigation_id="inv-INC1"):
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


_DDL = """
CREATE TABLE IF NOT EXISTS incident_graph_nodes (
    node_id      TEXT NOT NULL,
    incident_id  TEXT NOT NULL,
    node_type    TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL DEFAULT '',
    service      TEXT NOT NULL DEFAULT '',
    properties   TEXT NOT NULL DEFAULT '{}',
    recorded_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (node_id, incident_id)
);
CREATE TABLE IF NOT EXISTS incident_graph_edges (
    edge_id         TEXT PRIMARY KEY,
    incident_id     TEXT NOT NULL DEFAULT '',
    source_node_id  TEXT NOT NULL DEFAULT '',
    target_node_id  TEXT NOT NULL DEFAULT '',
    relationship    TEXT NOT NULL DEFAULT '',
    weight          REAL NOT NULL DEFAULT 1.0,
    properties      TEXT NOT NULL DEFAULT '{}',
    recorded_at     TEXT NOT NULL DEFAULT ''
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


def _seed_node(
    *,
    node_id: str,
    incident_id: str,
    service: str,
    node_type: str = "service",
    label: str = "svc",
    recorded_at: str = "2026-07-01T00:00:00Z",
) -> None:
    conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
    conn.execute(
        "INSERT INTO incident_graph_nodes "
        "(node_id, incident_id, node_type, label, service, properties, recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (node_id, incident_id, node_type, label, service, "{}", recorded_at),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

class TestSpec:
    def test_spec_name(self):
        assert INCIDENT_GRAPH_LOOKUP_SPEC.name == "incident_graph_lookup"

    def test_spec_stage(self):
        assert INCIDENT_GRAPH_LOOKUP_SPEC.stage == IntelligenceStage.POST_CLASSIFY

    def test_spec_feature_flag(self):
        assert INCIDENT_GRAPH_LOOKUP_SPEC.feature_flag == INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG
        assert INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG == "ENABLE_INCIDENT_GRAPH_LOOKUP"

    def test_spec_priority(self):
        assert INCIDENT_GRAPH_LOOKUP_SPEC.priority == 300


# ---------------------------------------------------------------------------
# Skip / Empty
# ---------------------------------------------------------------------------

class TestSkip:
    def test_no_service_skips(self):
        ctx = RuntimeContext(
            investigation_id="inv-none",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"incident": {"incident_id": "INC1"}, "service": ""},
            cres=_FakeCres(incident_type="whatever"),
        )
        out = incident_graph_lookup_runner(ctx)
        assert out["status"] == "skipped"
        assert out["reason"] == "no_service"


class TestEmpty:
    def test_empty_store_returns_success_empty_list(self):
        out = incident_graph_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["related_incident_ids"] == []
        assert out["related_incident_count"] == 0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class TestMatching:
    def test_finds_other_incidents_on_same_service(self):
        _seed_node(node_id="n1", incident_id="INC_OLDER_1", service="checkout")
        _seed_node(node_id="n2", incident_id="INC_OLDER_2", service="checkout")
        out = incident_graph_lookup_runner(_ctx(incident_id="INC_CURRENT"))
        assert out["status"] == "success"
        assert set(out["related_incident_ids"]) == {"INC_OLDER_1", "INC_OLDER_2"}

    def test_excludes_current_incident(self):
        # Same node inserted for the CURRENT incident id
        _seed_node(node_id="curr", incident_id="INC_CURRENT", service="checkout")
        _seed_node(node_id="prev", incident_id="INC_OLDER", service="checkout")
        out = incident_graph_lookup_runner(_ctx(incident_id="INC_CURRENT"))
        assert "INC_CURRENT" not in out["related_incident_ids"]
        assert "INC_OLDER" in out["related_incident_ids"]

    def test_only_matches_same_service(self):
        _seed_node(node_id="wrong", incident_id="INC_OTHER", service="payments")
        out = incident_graph_lookup_runner(_ctx(service="checkout"))
        assert out["related_incident_ids"] == []

    def test_bounded_by_max(self):
        for i in range(_MAX_RELATED_INCIDENTS + 5):
            _seed_node(
                node_id=f"n-{i:02d}",
                incident_id=f"INC-OLD-{i:02d}",
                service="checkout",
                recorded_at=f"2026-06-{i+1:02d}T00:00:00Z",
            )
        out = incident_graph_lookup_runner(_ctx(incident_id="INC_CURRENT"))
        assert len(out["related_incident_ids"]) == _MAX_RELATED_INCIDENTS
        assert out["related_incident_count"] == _MAX_RELATED_INCIDENTS


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "incident_graph_lookup")
        assert entry.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "incident_graph_lookup")
        assert entry.status == "success"
        assert entry.metadata["version"] == LOOKUP_VERSION


# ---------------------------------------------------------------------------
# Receipt lift
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_metadata_flows_to_receipt(self, monkeypatch):
        monkeypatch.setenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        _seed_node(node_id="lift", incident_id="INC_LIFT", service="checkout")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("classify") as _r:
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY,
                                    _ctx(incident_id="INC_CURRENT"))
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        entry = next(e for e in col.to_list()[0]["metadata"]["intelligence"]
                       if e["name"] == "incident_graph_lookup")
        assert entry["status"] == "success"
        assert "INC_LIFT" in entry["metadata"]["related_incident_ids"]


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_query_failure_returns_empty_matches(self, monkeypatch):
        monkeypatch.setenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        from intelligence import incident_graph as _ig
        with patch.object(_ig.IncidentGraphStore, "find_related_incidents",
                           side_effect=RuntimeError("db offline")):
            out = incident_graph_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["related_incident_ids"] == []

    def test_runtime_catches_unexpected(self, monkeypatch):
        monkeypatch.setenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.intelligence_modules import incident_graph_lookup as _igl
        with patch.object(_igl, "_extract_service",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "incident_graph_lookup")
        assert entry.status == "failed"
        assert entry.error_type == "ValueError"


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_no_writes_to_incident_graph_tables(self, monkeypatch):
        monkeypatch.setenv(INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        _seed_node(node_id="ro", incident_id="INC_RO", service="checkout")
        conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
        n0 = conn.execute("SELECT COUNT(*) FROM incident_graph_nodes").fetchone()[0]
        e0 = conn.execute("SELECT COUNT(*) FROM incident_graph_edges").fetchone()[0]
        conn.close()
        incident_graph_lookup_runner(_ctx())
        conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
        n1 = conn.execute("SELECT COUNT(*) FROM incident_graph_nodes").fetchone()[0]
        e1 = conn.execute("SELECT COUNT(*) FROM incident_graph_edges").fetchone()[0]
        conn.close()
        assert (n0, e0) == (n1, e1)


# ---------------------------------------------------------------------------
# Agent wiring + JSON safety
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_registered(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("incident_graph_lookup")

    def test_in_post_classify_plan(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_CLASSIFY)
        assert any(s.name == "incident_graph_lookup" for s in specs)

    def test_agent_source_untouched(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "install_default_modules(_intel)" in src
        assert "incident_graph_lookup" not in src


class TestJsonSafety:
    def test_payload_json_roundtrip(self):
        _seed_node(node_id="j1", incident_id="INC_J1", service="checkout")
        out = incident_graph_lookup_runner(_ctx(incident_id="INC_CURR"))
        s = json.dumps(out)
        d = json.loads(s)
        assert d["status"] == "success"
        assert "INC_J1" in d["related_incident_ids"]
