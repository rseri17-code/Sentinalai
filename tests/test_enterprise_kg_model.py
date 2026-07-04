"""Pure-library tests for sentinel_core.models.knowledge_graph.

Deterministic transform: IntelligenceContext → KnowledgeGraph.
Zero I/O, zero fixtures with side effects, zero LLM invocations.
"""
from __future__ import annotations

import json
import pytest

from sentinel_core.models.intel_context import (
    AffectedService,
    DependencyEdge,
    EpisodeMatch,
    IntelligenceContext,
    InvestigationMatch,
    PatternMatch,
    ResolutionMemoryMatch,
)
from sentinel_core.models.knowledge_graph import (
    EdgeType,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeGraphBuilder,
    KnowledgeNode,
    NodeType,
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Taxonomy enums
# ---------------------------------------------------------------------------

class TestTaxonomy:
    def test_node_type_covers_mission_entities(self):
        for entity in ("application", "service", "namespace", "pod", "node",
                         "cluster", "database", "gateway", "load_balancer", "api",
                         "kafka", "queue", "dns", "certificate", "host", "vm",
                         "cloud_resource", "aws_account", "azure_subscription",
                         "service_owner", "support_team", "business_service",
                         "incident", "change", "deployment", "runbook", "dashboard",
                         "alert", "transaction", "external_dependency", "pattern"):
            assert entity in {n.value for n in NodeType}

    def test_edge_type_covers_mission_relationships(self):
        for rel in ("supports", "depends_on", "calls", "owns", "hosted_on",
                      "runs_in", "connected_to", "protected_by", "observed_by",
                      "affected_by", "changed_by", "deploys_to", "resolves",
                      "consumes", "produces", "related_incident",
                      "historical_failure", "known_pattern", "known_blast_radius"):
            assert rel in {e.value for e in EdgeType}


# ---------------------------------------------------------------------------
# Node / edge factories
# ---------------------------------------------------------------------------

class TestFactories:
    def test_node_id_deterministic_by_type_and_label(self):
        a = KnowledgeNode.make(NodeType.SERVICE, "checkout")
        b = KnowledgeNode.make(NodeType.SERVICE, "checkout")
        assert a.node_id == b.node_id
        c = KnowledgeNode.make(NodeType.SERVICE, "payments")
        assert a.node_id != c.node_id

    def test_edge_id_deterministic(self):
        e1 = KnowledgeEdge.make("s1", "s2", EdgeType.DEPENDS_ON)
        e2 = KnowledgeEdge.make("s1", "s2", EdgeType.DEPENDS_ON)
        assert e1.edge_id == e2.edge_id

    def test_immutable(self):
        n = KnowledgeNode.make(NodeType.SERVICE, "s")
        with pytest.raises(Exception):
            n.label = "other"


# ---------------------------------------------------------------------------
# Empty IC → empty(-ish) graph
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_empty_ic_produces_empty_graph(self):
        g = KnowledgeGraphBuilder.from_intelligence_context(IntelligenceContext())
        assert g.node_count() == 0
        assert g.edge_count() == 0

    def test_only_incident_yields_incident_node_only(self):
        g = KnowledgeGraphBuilder.from_intelligence_context(
            IntelligenceContext(),
            incident_id="INC1", service="", incident_type="",
        )
        assert g.node_count() == 1
        assert g.nodes[0].node_type == "incident"

    def test_only_service_yields_service_node_only(self):
        g = KnowledgeGraphBuilder.from_intelligence_context(
            IntelligenceContext(),
            service="checkout",
        )
        assert g.node_count() == 1
        assert g.nodes[0].node_type == "service"
        assert g.nodes[0].label == "checkout"

    def test_none_ic_treated_as_empty(self):
        g = KnowledgeGraphBuilder.from_intelligence_context(
            None,
            incident_id="INC1", service="checkout", incident_type="saturation",
        )
        # Central service + incident + connecting edge
        assert g.node_count() == 2
        assert g.edge_count() == 1
        assert g.edges[0].edge_type == "affected_by"


# ---------------------------------------------------------------------------
# Full transform
# ---------------------------------------------------------------------------

class TestFullTransform:
    def test_all_sources_populate_expected_types(self):
        ic = IntelligenceContext(
            service="checkout", incident_type="saturation",
            resolution_memory_matches=(
                ResolutionMemoryMatch(memory_id="m1", root_cause_head="db pool",
                                        confidence=82, recorded_at="2026-07",
                                        service="checkout", incident_type="saturation"),
            ),
            investigation_matches=(
                InvestigationMatch(investigation_id="inv-old-1",
                                     created_at="2026-06",
                                     incident_type="saturation",
                                     service="checkout"),
            ),
            pattern_matches=(
                PatternMatch(pattern_id="p1", incident_type="db_pool",
                              services=["checkout"], canonical_symptoms=["db","pool"],
                              occurrence_count=5, success_count=4, success_rate=0.8),
            ),
            related_incident_ids=("INC_A", "INC_B"),
            upstream_dependencies=(DependencyEdge(source_service="checkout",
                                                    target_service="db",
                                                    dep_type="runtime",
                                                    strength=0.9),),
            downstream_dependents=(DependencyEdge(source_service="cart-api",
                                                    target_service="checkout",
                                                    dep_type="runtime",
                                                    strength=0.8),),
            affected_services=("cart-api",),
            episode_matches=(EpisodeMatch(episode_id="e1", incident_id="INC1",
                                            service="checkout",
                                            incident_type="saturation",
                                            root_cause_head="cause",
                                            resolution_action_head="restart",
                                            outcome="resolved",
                                            confidence=0.9,
                                            recorded_at="2026-07"),),
            blast_radius_severity="high",
            blast_radius_total_affected=2,
            blast_radius_affected=(AffectedService(service_id="ui-web",
                                                     probability=0.7,
                                                     propagation_ms=100),),
            module_names_seen=("historical_lookup", "pattern_recognition",
                                 "incident_graph_lookup",
                                 "dependency_graph_lookup",
                                 "episodic_memory_lookup",
                                 "causal_graph_lookup"),
        )
        g = KnowledgeGraphBuilder.from_intelligence_context(
            ic, incident_id="INC1", service="checkout",
            incident_type="saturation", root_cause="db pool exhausted",
            remediation_action="scale pool",
        )
        types = {n.node_type for n in g.nodes}
        assert "service" in types
        assert "incident" in types
        assert "pattern" in types

        etypes = {e.edge_type for e in g.edges}
        assert "affected_by" in etypes
        assert "historical_failure" in etypes
        assert "related_incident" in etypes
        assert "known_pattern" in etypes
        assert "depends_on" in etypes
        assert "known_blast_radius" in etypes

        svc = g.nodes_by_type(NodeType.SERVICE)
        primary = [n for n in svc if n.label == "checkout"][0]
        props = primary.properties
        assert props["historical_failures"] == 3
        assert set(props["known_incident_types"]) >= {"saturation", "db_pool"}
        assert props["known_rca"] == "db pool"
        assert "restart" in props["known_fixes"]
        assert props["blast_radius"]["severity"] == "high"
        assert "db" in props["upstream"]
        assert "cart-api" in props["downstream"]
        assert props["confidence"] == 82
        assert 40 <= props["health_score"] <= 70
        assert props["recurrence"] is True
        assert "historical_lookup" in props["evidence_sources"]


# ---------------------------------------------------------------------------
# Serialization determinism
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_same_input_yields_byte_identical_output(self):
        ic = IntelligenceContext(
            service="checkout", incident_type="t",
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=2, success_count=1,
                                            success_rate=0.5),),
        )
        g1 = KnowledgeGraphBuilder.from_intelligence_context(
            ic, incident_id="INC1", service="checkout")
        g2 = KnowledgeGraphBuilder.from_intelligence_context(
            ic, incident_id="INC1", service="checkout")
        assert json.dumps(g1.to_dict(), sort_keys=True) \
            == json.dumps(g2.to_dict(), sort_keys=True)

    def test_to_dict_shape(self):
        g = KnowledgeGraphBuilder.from_intelligence_context(
            IntelligenceContext(),
            incident_id="INC1", service="s", incident_type="t",
        )
        d = g.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION
        assert d["node_count"] == 2
        assert d["edge_count"] == 1
        assert isinstance(d["nodes"], list)
        assert isinstance(d["edges"], list)

    def test_nodes_sorted_by_id_in_output(self):
        ic = IntelligenceContext(
            service="checkout",
            upstream_dependencies=(DependencyEdge(source_service="checkout",
                                                    target_service="aaa",
                                                    dep_type="runtime",
                                                    strength=0.5),
                                     DependencyEdge(source_service="checkout",
                                                    target_service="zzz",
                                                    dep_type="runtime",
                                                    strength=0.5),),
        )
        g = KnowledgeGraphBuilder.from_intelligence_context(ic, service="checkout")
        d = g.to_dict()
        ids = [n["node_id"] for n in d["nodes"]]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Merge / dedup
# ---------------------------------------------------------------------------

class TestMerge:
    def test_duplicate_upstream_target_is_deduped(self):
        ic = IntelligenceContext(
            service="checkout",
            upstream_dependencies=(
                DependencyEdge(source_service="checkout", target_service="db",
                                dep_type="runtime", strength=0.5),
                DependencyEdge(source_service="checkout", target_service="db",
                                dep_type="runtime", strength=0.9),
            ),
        )
        g = KnowledgeGraphBuilder.from_intelligence_context(ic, service="checkout")
        svcs = g.nodes_by_type(NodeType.SERVICE)
        db_nodes = [n for n in svcs if n.label == "db"]
        assert len(db_nodes) == 1
        depends_edges = g.edges_by_type(EdgeType.DEPENDS_ON)
        assert len({(e.source_id, e.target_id) for e in depends_edges}) == 1


# ---------------------------------------------------------------------------
# Robustness — malformed input
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_dict_like_input_supported(self):
        g = KnowledgeGraphBuilder.from_intelligence_context({
            "service": "checkout",
        }, incident_id="INC1", service="checkout")
        assert g.node_count() == 2

    def test_bad_types_do_not_crash(self):
        class _Mock:
            service = "checkout"
            incident_type = 42
            resolution_memory_matches = "not-a-tuple"
            pattern_matches = None
            related_incident_ids = None
            upstream_dependencies = None
            downstream_dependents = None
            blast_radius_severity = None
            blast_radius_total_affected = "not-an-int"
            blast_radius_affected = None
            episode_matches = None
            module_names_seen = None
            investigation_matches = None
            affected_services = None
        g = KnowledgeGraphBuilder.from_intelligence_context(
            _Mock(), incident_id="INC1", service="checkout")
        assert g.node_count() == 2
        assert g.edge_count() == 1


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

class TestAccessors:
    def test_find_node(self):
        g = KnowledgeGraphBuilder.from_intelligence_context(
            IntelligenceContext(), service="checkout")
        svc = g.nodes[0]
        assert g.find_node(svc.node_id) is svc
        assert g.find_node("nope") is None

    def test_by_type_helpers(self):
        ic = IntelligenceContext(
            service="checkout",
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=2, success_count=1,
                                            success_rate=0.5),),
        )
        g = KnowledgeGraphBuilder.from_intelligence_context(
            ic, service="checkout")
        assert len(g.nodes_by_type(NodeType.PATTERN)) == 1
        assert len(g.nodes_by_type("service")) == 1
        assert len(g.edges_by_type(EdgeType.KNOWN_PATTERN)) == 1
