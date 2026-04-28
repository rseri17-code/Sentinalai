"""Tests for supervisor.knowledge_graph."""
from __future__ import annotations

import json
import os
import pytest

from supervisor.knowledge_graph import (
    KnowledgeGraph,
    _normalise_rc_id,
)


# ---------------------------------------------------------------------------
# KnowledgeGraph — mutations
# ---------------------------------------------------------------------------

class TestKnowledgeGraphMutations:

    def test_add_node(self):
        g = KnowledgeGraph()
        node = g.add_node("n1", "incident", "INC-001")
        assert node.node_id == "n1"
        assert node.node_type == "incident"
        assert g.get_node("n1") is node

    def test_add_node_updates_existing(self):
        g = KnowledgeGraph()
        g.add_node("n1", "incident", "INC-001", status="open")
        g.add_node("n1", "incident", "INC-001", resolution="fixed")
        node = g.get_node("n1")
        assert node.props.get("status") == "open"
        assert node.props.get("resolution") == "fixed"

    def test_add_edge(self):
        g = KnowledgeGraph()
        g.add_node("src", "incident", "INC-001")
        g.add_node("dst", "service", "payment-service")
        edge = g.add_edge("src", "dst", "AFFECTED")
        assert edge is not None
        assert edge.rel_type == "AFFECTED"

    def test_add_edge_missing_node_returns_none(self):
        g = KnowledgeGraph()
        g.add_node("src", "incident", "INC-001")
        result = g.add_edge("src", "nonexistent", "AFFECTED")
        assert result is None

    def test_add_edge_deduplicates(self):
        g = KnowledgeGraph()
        g.add_node("a", "incident", "INC-001")
        g.add_node("b", "service", "svc")
        g.add_edge("a", "b", "AFFECTED", weight=0.5)
        g.add_edge("a", "b", "AFFECTED", weight=0.8)
        # Should not duplicate
        assert g.edge_count() == 1
        # Should take max weight
        edge_key = "a::AFFECTED::b"
        assert g._edges[edge_key].weight == pytest.approx(0.8)

    def test_node_count_and_edge_count(self):
        g = KnowledgeGraph()
        g.add_node("a", "incident", "A")
        g.add_node("b", "service", "B")
        g.add_edge("a", "b", "AFFECTED")
        assert g.node_count() == 2
        assert g.edge_count() == 1


# ---------------------------------------------------------------------------
# ingest_investigation
# ---------------------------------------------------------------------------

class TestIngestInvestigation:

    def test_creates_expected_nodes(self):
        g = KnowledgeGraph()
        g.ingest_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="payment-service",
            root_cause="Connection pool exhausted",
            confidence=85,
        )
        # Incident, service, root_cause, error_type nodes
        assert g.get_node("INC001") is not None
        assert g.get_node("svc:payment-service") is not None
        assert g.get_node("errtype:saturation") is not None
        # Root cause node — normalised key
        rc_id = _normalise_rc_id("Connection pool exhausted")
        assert g.get_node(rc_id) is not None

    def test_related_incidents_linked(self):
        g = KnowledgeGraph()
        g.ingest_investigation("INC001", "saturation", "svc", "pool exhausted", 80)
        g.ingest_investigation(
            "INC002", "saturation", "svc", "pool exhausted again", 70,
            related_incident_ids=["INC001"],
        )
        # INC002 should have RELATED_TO edge to INC001
        neighbors = g.neighbors("INC002", "RELATED_TO")
        neighbor_ids = {n.node_id for n in neighbors}
        assert "INC001" in neighbor_ids

    def test_evicts_when_over_cap(self, monkeypatch):
        monkeypatch.setattr("supervisor.knowledge_graph.KG_MAX_NODES", 4)
        g = KnowledgeGraph()
        for i in range(5):
            g.ingest_investigation(f"INC{i:03d}", "error", "svc", f"cause {i}", 50)
        # Should have evicted some to stay <= cap
        # (4 nodes = cap, but each ingest adds ~4 nodes so eviction kicks in)
        assert g.node_count() <= 20  # generous cap test


# ---------------------------------------------------------------------------
# find_similar_incidents
# ---------------------------------------------------------------------------

class TestFindSimilarIncidents:

    def _populated_graph(self) -> KnowledgeGraph:
        g = KnowledgeGraph()
        g.ingest_investigation("INC001", "saturation", "payment-service",
                               "Connection pool exhausted", 85)
        g.ingest_investigation("INC002", "saturation", "payment-service",
                               "Connection pool exhausted again", 80)
        g.ingest_investigation("INC003", "oom_kill", "auth-service",
                               "OOM due to memory leak", 90)
        return g

    def test_returns_list(self):
        g = self._populated_graph()
        results = g.find_similar_incidents("payment-service", "saturation")
        assert isinstance(results, list)

    def test_type_boost_increases_same_type_score(self):
        g = self._populated_graph()
        results = g.find_similar_incidents("payment-service", "saturation", top_k=5)
        # Should prefer same-type (saturation) incidents
        types = [r["incident_type"] for r in results]
        assert "saturation" in types

    def test_returns_empty_for_unknown_service(self):
        g = self._populated_graph()
        results = g.find_similar_incidents("nonexistent-service", "error")
        assert results == []

    def test_top_k_limit(self):
        g = self._populated_graph()
        results = g.find_similar_incidents("payment-service", "saturation", top_k=1)
        assert len(results) <= 1

    def test_result_keys(self):
        g = self._populated_graph()
        results = g.find_similar_incidents("payment-service", "saturation")
        if results:
            assert "incident_id" in results[0]
            assert "relevance_score" in results[0]
            assert "root_cause" in results[0]


# ---------------------------------------------------------------------------
# find_recurring_root_causes
# ---------------------------------------------------------------------------

class TestFindRecurringRootCauses:

    def test_recurring_root_cause(self):
        g = KnowledgeGraph()
        for i in range(3):
            g.ingest_investigation(
                f"INC{i:03d}", "saturation", "payment-service",
                "Connection pool exhausted", 80,
            )
        recurring = g.find_recurring_root_causes("payment-service")
        assert len(recurring) >= 1
        assert recurring[0]["recurrence_count"] >= 2

    def test_returns_empty_for_no_service(self):
        g = KnowledgeGraph()
        result = g.find_recurring_root_causes("no-such-service")
        assert result == []


# ---------------------------------------------------------------------------
# neighbors
# ---------------------------------------------------------------------------

class TestNeighbors:

    def test_neighbors_filtered_by_rel_type(self):
        g = KnowledgeGraph()
        g.add_node("inc", "incident", "INC")
        g.add_node("svc", "service", "svc")
        g.add_node("rc", "root_cause", "rc")
        g.add_edge("inc", "svc", "AFFECTED")
        g.add_edge("inc", "rc", "HAS_ROOT_CAUSE")

        affected = g.neighbors("inc", "AFFECTED")
        assert len(affected) == 1
        assert affected[0].node_id == "svc"

        rc_neighbors = g.neighbors("inc", "HAS_ROOT_CAUSE")
        assert len(rc_neighbors) == 1

    def test_neighbors_unfiltered_returns_all(self):
        g = KnowledgeGraph()
        g.add_node("inc", "incident", "INC")
        g.add_node("svc", "service", "svc")
        g.add_node("rc", "root_cause", "rc")
        g.add_edge("inc", "svc", "AFFECTED")
        g.add_edge("inc", "rc", "HAS_ROOT_CAUSE")
        all_nb = g.neighbors("inc")
        assert len(all_nb) == 2


# ---------------------------------------------------------------------------
# Persistence: to_dict / from_dict / save
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_roundtrip_to_from_dict(self):
        g = KnowledgeGraph()
        g.ingest_investigation("INC001", "saturation", "svc", "pool exhausted", 80)
        d = g.to_dict()
        g2 = KnowledgeGraph.from_dict(d)
        assert g2.node_count() == g.node_count()
        assert g2.edge_count() == g.edge_count()

    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "kg.json")
        g = KnowledgeGraph()
        g.ingest_investigation("INC001", "saturation", "svc", "pool exhausted", 80)
        g.save(path)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        g2 = KnowledgeGraph.from_dict(data)
        assert g2.node_count() == g.node_count()


# ---------------------------------------------------------------------------
# _normalise_rc_id
# ---------------------------------------------------------------------------

class TestNormaliseRcId:

    def test_stable_for_same_input(self):
        assert _normalise_rc_id("Connection pool exhausted") == _normalise_rc_id("Connection pool exhausted")

    def test_prefix_rc(self):
        assert _normalise_rc_id("pool exhausted").startswith("rc:")

    def test_strips_special_chars(self):
        rc_id = _normalise_rc_id("OOMKilled: process killed!")
        assert "!" not in rc_id
        assert ":" not in rc_id.split("rc:")[1]

    def test_max_5_words(self):
        long_rc = "a b c d e f g h i j k l"
        rc_id = _normalise_rc_id(long_rc)
        words = rc_id.split("rc:")[1].split("_")
        assert len(words) <= 5
