"""Cross-Incident Causal Graph — comprehensive tests."""
from __future__ import annotations

import json

import pytest

from sentinel_core.causal_graph import (
    CAUSAL_SCHEMA_VERSION,
    CausalChain,
    CausalEdge,
    CausalEdgeType,
    CausalGraph,
    CausalGraphBuilder,
    CausalNode,
    CausalNodeType,
    CausalPath,
    CausalRecommendation,
    CausalRecommendationEngine,
    CausalRecommendationKind,
    CausalRecurrence,
    ChainDetector,
    MTTIPath,
    MTTIPathRanker,
    RCAPath,
    RCAPathRanker,
    RecurrenceDetector,
    make_edge_id,
    make_node_id,
    render_causal_chains,
    render_causal_graph,
    render_causal_recommendations,
    render_master_report,
    render_mtti_paths,
    render_rca_paths,
    render_recurrence_report,
    render_service_causal_profile,
    to_json,
)
from sentinel_core.intel_memory import (
    BlastRadiusSnapshot,
    MemoryRecord,
    TopologySnapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _rec(mid, **k):
    d = dict(memory_id=mid)
    d.update(k)
    return MemoryRecord(**d)


def _corpus() -> tuple[MemoryRecord, ...]:
    return (
        _rec("m1",
              incident_id="INC1",
              service="checkout", application="ecom",
              incident_type="saturation",
              detected_root_cause="database pool exhausted",
              resolution="scale pool",
              evidence_collected=("oom_events", "logs", "metrics_red"),
              evidence_ordering=("oom_events", "logs", "metrics_red"),
              planner_decisions=("cap:collect_pod_lifecycle", "cap:collect_logs"),
              mtti_ms=45000, confidence=88,
              investigation_score=0.9,
              false_leads=("certificate",),
              skills_used=("kubectl_pods",),
              topology=TopologySnapshot(
                  services=("checkout", "db"),
                  dependencies=(("checkout", "db"),))),
        _rec("m2",
              incident_id="INC2",
              service="checkout", application="ecom",
              incident_type="saturation",
              detected_root_cause="database pool exhausted",
              resolution="scale pool",
              evidence_collected=("oom_events", "logs"),
              evidence_ordering=("oom_events", "logs"),
              planner_decisions=("cap:collect_pod_lifecycle", "cap:collect_logs"),
              mtti_ms=52000, confidence=82,
              investigation_score=0.85,
              false_leads=("certificate",)),
        _rec("m3",
              incident_id="INC3",
              service="payments", application="fintech",
              incident_type="network",
              detected_root_cause="dns nxdomain",
              resolution="reload coredns",
              evidence_collected=("dns_records",),
              mtti_ms=90000, confidence=60,
              investigation_score=0.5),
    )


# ---------------------------------------------------------------------------
# Node + edge basics
# ---------------------------------------------------------------------------

class TestNodeEdge:
    def test_node_id_deterministic(self):
        a = make_node_id(CausalNodeType.INCIDENT, "INC1")
        b = make_node_id(CausalNodeType.INCIDENT, "INC1")
        assert a == b
        assert a != make_node_id(CausalNodeType.INCIDENT, "INC2")

    def test_edge_id_deterministic(self):
        a = make_edge_id("s", "t", CausalEdgeType.CAUSED_BY)
        b = make_edge_id("s", "t", CausalEdgeType.CAUSED_BY)
        assert a == b

    def test_node_frozen(self):
        n = CausalNode.make(CausalNodeType.INCIDENT, "INC1")
        with pytest.raises(Exception):
            n.label = "x"

    def test_edge_frozen(self):
        e = CausalEdge.make("s", "t", CausalEdgeType.CAUSED_BY)
        with pytest.raises(Exception):
            e.weight = 0.5

    def test_all_node_types_present(self):
        for t in ("incident", "service", "symptom", "signal", "hypothesis",
                    "evidence", "root_cause", "remediation",
                    "deployment_change", "dependency", "failure_mode",
                    "incident_pattern"):
            assert t in {n.value for n in CausalNodeType}

    def test_all_edge_types_present(self):
        for t in ("observed_in", "caused_by", "supports", "disproves",
                    "precedes", "correlates_with", "resolved_by", "affects",
                    "depends_on", "recurs_with", "reduces_mtti",
                    "increases_confidence"):
            assert t in {e.value for e in CausalEdgeType}


# ---------------------------------------------------------------------------
# CausalGraph container
# ---------------------------------------------------------------------------

class TestCausalGraph:
    def test_empty(self):
        g = CausalGraph()
        assert g.node_count() == 0
        assert g.edge_count() == 0

    def test_nodes_by_type(self):
        n = CausalNode.make(CausalNodeType.INCIDENT, "INC1")
        g = CausalGraph(nodes=(n,))
        assert g.nodes_by_type("incident") == (n,)

    def test_to_dict_sorted(self):
        n1 = CausalNode.make(CausalNodeType.INCIDENT, "b")
        n2 = CausalNode.make(CausalNodeType.INCIDENT, "a")
        g = CausalGraph(nodes=(n1, n2))
        d = g.to_dict()
        ids = [n["node_id"] for n in d["nodes"]]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

class TestGraphBuilder:
    def test_empty_corpus(self):
        g = CausalGraphBuilder().build(())
        assert g.node_count() == 0

    def test_populates_multiple_node_types(self):
        g = CausalGraphBuilder().build(_corpus())
        types = {n.node_type for n in g.nodes}
        assert "incident" in types
        assert "service" in types
        assert "root_cause" in types
        assert "evidence" in types
        assert "failure_mode" in types
        assert "remediation" in types

    def test_recurs_with_between_shared_root_cause(self):
        g = CausalGraphBuilder().build(_corpus())
        recurs = g.edges_by_type("recurs_with")
        # m1 and m2 share root cause → at least one RECURS_WITH edge
        assert len(recurs) >= 1

    def test_deterministic(self):
        g1 = CausalGraphBuilder().build(_corpus())
        g2 = CausalGraphBuilder().build(_corpus())
        assert g1.to_dict() == g2.to_dict()

    def test_dependencies_produce_dep_nodes(self):
        g = CausalGraphBuilder().build(_corpus())
        dep_labels = {n.label for n in g.nodes_by_type("dependency")}
        assert "checkout->db" in dep_labels


# ---------------------------------------------------------------------------
# Chain detector
# ---------------------------------------------------------------------------

class TestChainDetector:
    def test_empty(self):
        assert ChainDetector().detect(()) == ()

    def test_shared_chain_detected(self):
        chains = ChainDetector(min_count=2).detect(_corpus())
        assert len(chains) == 1   # m1, m2 share (svc, type, rc, resolution)
        assert chains[0].count == 2

    def test_min_count_respected(self):
        chains = ChainDetector(min_count=3).detect(_corpus())
        assert chains == ()

    def test_confidence_averaged(self):
        chains = ChainDetector().detect(_corpus())
        assert chains[0].confidence > 0.0

    def test_deterministic(self):
        c1 = ChainDetector().detect(_corpus())
        c2 = ChainDetector().detect(_corpus())
        assert [x.to_dict() for x in c1] == [x.to_dict() for x in c2]


# ---------------------------------------------------------------------------
# Recurrence detector
# ---------------------------------------------------------------------------

class TestRecurrenceDetector:
    def test_by_root_cause(self):
        r = RecurrenceDetector().by_root_cause(_corpus())
        assert len(r) == 1
        assert r[0].count == 2
        assert r[0].kind == "root_cause"

    def test_by_service(self):
        r = RecurrenceDetector().by_service(_corpus())
        # checkout appears twice
        by_sig = {x.signature: x.count for x in r}
        assert by_sig.get("checkout") == 2

    def test_all_recurrences_includes_multiple_kinds(self):
        r = RecurrenceDetector().all_recurrences(_corpus())
        kinds = {x.kind for x in r}
        assert "root_cause" in kinds
        assert "service" in kinds


# ---------------------------------------------------------------------------
# RCA paths
# ---------------------------------------------------------------------------

class TestRCAPathRanker:
    def test_ranker_produces_paths(self):
        paths = RCAPathRanker().build(_corpus())
        assert any(p.service == "checkout" for p in paths)

    def test_recurrence_sorted_first(self):
        paths = RCAPathRanker().build(_corpus())
        # checkout+saturation+db pool has recurrence 2 → first
        assert paths[0].recurrence >= paths[-1].recurrence

    def test_evidence_keys_populated(self):
        paths = RCAPathRanker().build(_corpus())
        assert any(len(p.evidence_keys) > 0 for p in paths)

    def test_deterministic(self):
        p1 = RCAPathRanker().build(_corpus())
        p2 = RCAPathRanker().build(_corpus())
        assert [x.to_dict() for x in p1] == [x.to_dict() for x in p2]


# ---------------------------------------------------------------------------
# MTTI paths
# ---------------------------------------------------------------------------

class TestMTTIPathRanker:
    def test_paths_built(self):
        paths = MTTIPathRanker().build(_corpus())
        assert len(paths) >= 1

    def test_best_mtti_asc(self):
        paths = MTTIPathRanker().build(_corpus())
        best = [p.best_mtti_ms for p in paths if p.best_mtti_ms > 0]
        assert best == sorted(best)


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

class TestRecommendationEngine:
    def test_empty(self):
        assert CausalRecommendationEngine().recommend(()) == ()

    def test_produces_recommendations(self):
        r = CausalRecommendationEngine().recommend(_corpus())
        assert r
        assert all(len(x.evidence) > 0 for x in r)

    def test_recurring_root_cause_present(self):
        r = CausalRecommendationEngine().recommend(_corpus())
        kinds = {x.kind for x in r}
        assert CausalRecommendationKind.RECURRING_ROOT_CAUSE.value in kinds

    def test_deterministic(self):
        r1 = CausalRecommendationEngine().recommend(_corpus())
        r2 = CausalRecommendationEngine().recommend(_corpus())
        assert [x.to_dict() for x in r1] == [x.to_dict() for x in r2]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestReports:
    def test_causal_graph_report(self):
        r = render_causal_graph(_corpus())
        assert r["graph"]["node_count"] > 0

    def test_causal_chains_report(self):
        r = render_causal_chains(_corpus())
        assert r["chain_count"] == 1

    def test_rca_paths_report(self):
        r = render_rca_paths(_corpus())
        assert r["path_count"] >= 2

    def test_mtti_paths_report(self):
        r = render_mtti_paths(_corpus())
        assert r["path_count"] >= 2

    def test_recurrence_report(self):
        r = render_recurrence_report(_corpus())
        assert r["recurrence_count"] >= 2

    def test_service_causal_profile(self):
        r = render_service_causal_profile(_corpus())
        services = {p["service"] for p in r["profiles"]}
        assert "checkout" in services

    def test_causal_recommendations_report(self):
        r = render_causal_recommendations(_corpus())
        assert r["recommendation_count"] > 0

    def test_master_report_deterministic(self):
        j1 = to_json(render_master_report(_corpus()))
        j2 = to_json(render_master_report(_corpus()))
        assert j1 == j2
        json.loads(j1)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_no_forbidden_imports(self):
        import importlib
        for name in ("sentinel_core.causal_graph.schemas",
                      "sentinel_core.causal_graph.causal_node",
                      "sentinel_core.causal_graph.causal_edge",
                      "sentinel_core.causal_graph.graph_builder",
                      "sentinel_core.causal_graph.chain_detector",
                      "sentinel_core.causal_graph.recurrence",
                      "sentinel_core.causal_graph.rca_paths",
                      "sentinel_core.causal_graph.mtti_paths",
                      "sentinel_core.causal_graph.recommendation_engine",
                      "sentinel_core.causal_graph.report"):
            src = open(importlib.import_module(name).__file__).read()
            for banned in ("requests", "httpx", "urllib3", "boto3",
                             "openai", "anthropic", "supervisor.agent",
                             "kubernetes"):
                assert banned not in src, f"{name} imports {banned}"
