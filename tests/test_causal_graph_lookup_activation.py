"""CausalGraphLookup activation tests."""
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
from supervisor.intelligence_modules.causal_graph_lookup import (
    CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG,
    CAUSAL_GRAPH_LOOKUP_SPEC,
    LOOKUP_VERSION,
    _MAX_AFFECTED,
    causal_graph_lookup_runner,
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


def _node_line(**kw) -> str:
    return json.dumps({"_type": "node", "data": kw}) + "\n"


def _edge_line(**kw) -> str:
    return json.dumps({"_type": "edge", "data": kw}) + "\n"


@pytest.fixture(autouse=True)
def _isolate_graph(tmp_path, monkeypatch):
    """Point CAUSAL_GRAPH_PATH at a per-test JSONL file pre-seeded with the
    origin node so seed_demo_topology() does NOT fire (CausalGraph seeds
    only when _nodes is empty after load)."""
    path = tmp_path / "causal_graph.jsonl"
    with open(path, "w") as f:
        f.write(_node_line(
            service_id="checkout",
            display_name="Checkout Service",
            team="commerce",
            tier=1,
            health=0.9,
            alert_count=0,
            last_incident_ts="2026-07-01T00:00:00Z",
            technologies=["python", "postgres"],
        ))
    monkeypatch.setenv("CAUSAL_GRAPH_PATH", str(path))
    yield path


def _seed_downstream(path, src: str, tgt: str, correlation: float = 0.8) -> None:
    """Append a downstream node + edge from src → tgt."""
    with open(path, "a") as f:
        f.write(_node_line(
            service_id=tgt, display_name=tgt, team="ops", tier=2,
            health=0.9, alert_count=0, last_incident_ts="",
            technologies=[],
        ))
        f.write(_edge_line(
            source=src, target=tgt, edge_type="calls",
            call_volume=0.5, failure_correlation=correlation,
            avg_propagation_ms=100, observed_count=10,
            last_updated="2026-07-01T00:00:00Z",
        ))


class TestSpec:
    def test_name(self):
        assert CAUSAL_GRAPH_LOOKUP_SPEC.name == "causal_graph_lookup"

    def test_stage(self):
        assert CAUSAL_GRAPH_LOOKUP_SPEC.stage == IntelligenceStage.POST_CLASSIFY

    def test_feature_flag(self):
        assert CAUSAL_GRAPH_LOOKUP_SPEC.feature_flag == CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG
        assert CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG == "ENABLE_CAUSAL_GRAPH_LOOKUP"

    def test_priority(self):
        assert CAUSAL_GRAPH_LOOKUP_SPEC.priority == 600


class TestSkipAndEmpty:
    def test_no_service_skips(self):
        ctx = RuntimeContext(
            investigation_id="inv-x",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"service": ""},
            cres=_FakeCres(incident_type="x"),
        )
        out = causal_graph_lookup_runner(ctx)
        assert out["status"] == "skipped"
        assert out["reason"] == "no_service"

    def test_unknown_service_returns_success_low(self):
        # Service NOT in graph — CausalGraph.get_blast_radius returns empty result
        out = causal_graph_lookup_runner(_ctx(service="not-in-graph"))
        assert out["status"] == "success"
        assert out["severity"] == "low"
        assert out["total_affected"] == 0
        assert out["affected"] == []

    def test_known_service_no_downstream(self, _isolate_graph):
        # 'checkout' is seeded but has no downstream edges
        out = causal_graph_lookup_runner(_ctx(service="checkout"))
        assert out["status"] == "success"
        assert out["total_affected"] == 0


class TestBlastRadius:
    def test_single_downstream_appears(self, _isolate_graph):
        _seed_downstream(_isolate_graph, "checkout", "cart-api", correlation=0.9)
        out = causal_graph_lookup_runner(_ctx(service="checkout"))
        assert out["total_affected"] == 1
        assert out["affected"][0]["service_id"] == "cart-api"
        assert out["affected"][0]["probability"] == 0.9

    def test_severity_reflects_criticality(self, _isolate_graph):
        # tier=1 origin + 3 downstream ⇒ severity=critical
        _seed_downstream(_isolate_graph, "checkout", "a", 0.9)
        _seed_downstream(_isolate_graph, "checkout", "b", 0.9)
        _seed_downstream(_isolate_graph, "checkout", "c", 0.9)
        out = causal_graph_lookup_runner(_ctx(service="checkout"))
        assert out["severity"] == "critical"

    def test_affected_bounded(self, _isolate_graph):
        for i in range(_MAX_AFFECTED + 5):
            _seed_downstream(_isolate_graph, "checkout", f"svc-{i:02d}", 0.9)
        out = causal_graph_lookup_runner(_ctx(service="checkout"))
        assert len(out["affected"]) == _MAX_AFFECTED


class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        assert results == []

    def test_flag_off_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "causal_graph_lookup")
        assert entry.status == "skipped"

    def test_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "causal_graph_lookup")
        assert entry.status == "success"


class TestFailureIsolation:
    def test_blast_radius_failure_returns_low(self, monkeypatch):
        monkeypatch.setenv(CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        from intelligence import causal_graph as _cg
        with patch.object(_cg.CausalGraph, "get_blast_radius",
                           side_effect=RuntimeError("boom")):
            out = causal_graph_lookup_runner(_ctx())
        # Runner absorbs the failure — returns success with empty affected
        assert out["status"] == "success"
        assert out["severity"] == "low"
        assert out["total_affected"] == 0

    def test_runtime_catches_unexpected(self, monkeypatch):
        monkeypatch.setenv(CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.intelligence_modules import causal_graph_lookup as _cgl
        with patch.object(_cgl, "_extract_service",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "causal_graph_lookup")
        assert entry.status == "failed"


class TestReadOnly:
    def test_no_writes(self, _isolate_graph):
        _seed_downstream(_isolate_graph, "checkout", "cart-api", 0.7)
        before = open(_isolate_graph).read()
        causal_graph_lookup_runner(_ctx(service="checkout"))
        after = open(_isolate_graph).read()
        assert before == after


class TestAgentWiring:
    def test_registered(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("causal_graph_lookup")

    def test_agent_source_untouched(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "install_default_modules(_intel)" in src
        # Existing string on the legacy inline call — we DON'T touch that.
        # Just verify our new module isn't referenced.
        assert "causal_graph_lookup" not in src


class TestJsonSafety:
    def test_payload_roundtrip(self, _isolate_graph):
        _seed_downstream(_isolate_graph, "checkout", "cart-api", 0.8)
        out = causal_graph_lookup_runner(_ctx(service="checkout"))
        s = json.dumps(out)
        d = json.loads(s)
        assert d["status"] == "success"
        assert d["affected"][0]["service_id"] == "cart-api"
