"""Tests for intelligence.incident_graph — cross-investigation durable graph."""

import pytest
import sqlite3
import tempfile
import os

from intelligence.incident_graph import (
    IncidentGraphStore,
    IncidentNode,
    IncidentEdge,
    _node_id,
    _edge_id,
)


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_graph_nodes (
            node_id      TEXT NOT NULL,
            incident_id  TEXT NOT NULL,
            node_type    TEXT NOT NULL DEFAULT '',
            label        TEXT NOT NULL DEFAULT '',
            service      TEXT NOT NULL DEFAULT '',
            properties   TEXT NOT NULL DEFAULT '{}',
            recorded_at  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (node_id, incident_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_graph_edges (
            edge_id        TEXT PRIMARY KEY,
            incident_id    TEXT NOT NULL DEFAULT '',
            source_node_id TEXT NOT NULL DEFAULT '',
            target_node_id TEXT NOT NULL DEFAULT '',
            relationship   TEXT NOT NULL DEFAULT '',
            weight         REAL NOT NULL DEFAULT 1.0,
            properties     TEXT NOT NULL DEFAULT '{}',
            recorded_at    TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def db_path():
    path = _make_db()
    yield path
    os.unlink(path)


@pytest.fixture()
def store(db_path):
    return IncidentGraphStore(db_path)


# ── Deterministic IDs ──────────────────────────────────────────────────────────

def test_node_id_deterministic():
    id1 = _node_id("service", "api-gateway", "inc-001")
    id2 = _node_id("service", "api-gateway", "inc-001")
    assert id1 == id2
    assert len(id1) == 16


def test_edge_id_deterministic():
    id1 = _edge_id("src-node", "tgt-node", "CAUSED_BY", "inc-001")
    id2 = _edge_id("src-node", "tgt-node", "CAUSED_BY", "inc-001")
    assert id1 == id2
    assert len(id1) == 16


def test_node_id_differs_by_incident():
    id1 = _node_id("service", "api", "inc-001")
    id2 = _node_id("service", "api", "inc-002")
    assert id1 != id2


# ── make_node / make_edge ──────────────────────────────────────────────────────

def test_make_node(store):
    node = store.make_node("service", "api-gateway", "inc-001", service="api-gateway")
    assert node.node_type == "service"
    assert node.label == "api-gateway"
    assert node.incident_id == "inc-001"
    assert len(node.node_id) == 16
    assert node.recorded_at != ""


def test_make_edge(store):
    edge = store.make_edge("node-a", "node-b", "CAUSED_BY", "inc-001", weight=0.9)
    assert edge.relationship == "CAUSED_BY"
    assert edge.source_node_id == "node-a"
    assert edge.target_node_id == "node-b"
    assert edge.weight == 0.9
    assert len(edge.edge_id) == 16


# ── add_node / get_incident_nodes ──────────────────────────────────────────────

def test_add_and_retrieve_nodes(store):
    node = store.make_node("metric", "error_rate", "inc-002", service="api")
    store.add_node(node)
    nodes = store.get_incident_nodes("inc-002")
    assert len(nodes) == 1
    assert nodes[0].node_id == node.node_id
    assert nodes[0].node_type == "metric"


def test_add_node_idempotent(store):
    node = store.make_node("alert", "HighErrorRate", "inc-003", service="api")
    store.add_node(node)
    store.add_node(node)  # second insert must not raise
    nodes = store.get_incident_nodes("inc-003")
    assert len(nodes) == 1


def test_nodes_isolated_by_incident(store):
    n1 = store.make_node("service", "api", "inc-010")
    n2 = store.make_node("service", "api", "inc-011")
    store.add_node(n1)
    store.add_node(n2)
    assert len(store.get_incident_nodes("inc-010")) == 1
    assert len(store.get_incident_nodes("inc-011")) == 1


# ── add_edge / get_incident_edges ──────────────────────────────────────────────

def test_add_and_retrieve_edges(store):
    n1 = store.make_node("service", "api", "inc-004")
    n2 = store.make_node("outcome", "oom_kill", "inc-004")
    store.add_node(n1)
    store.add_node(n2)
    edge = store.make_edge(n1.node_id, n2.node_id, "CAUSED_BY", "inc-004")
    store.add_edge(edge)
    edges = store.get_incident_edges("inc-004")
    assert len(edges) == 1
    assert edges[0].relationship == "CAUSED_BY"


def test_add_edge_idempotent(store):
    edge = store.make_edge("n-a", "n-b", "CORRELATED", "inc-005")
    store.add_edge(edge)
    store.add_edge(edge)
    edges = store.get_incident_edges("inc-005")
    assert len(edges) == 1


# ── find_related_incidents ─────────────────────────────────────────────────────

def test_find_related_incidents_by_service(store):
    for inc in ["inc-a", "inc-b", "inc-c"]:
        node = store.make_node("service", "api-gateway", inc, service="api-gateway")
        store.add_node(node)
    # Different service
    store.add_node(store.make_node("service", "billing", "inc-d", service="billing"))

    related = store.find_related_incidents("api-gateway")
    assert set(related) == {"inc-a", "inc-b", "inc-c"}


def test_find_related_incidents_with_node_type_filter(store):
    store.add_node(store.make_node("alert", "HighCPU", "inc-e", service="compute"))
    store.add_node(store.make_node("metric", "cpu_usage", "inc-f", service="compute"))

    alerts_only = store.find_related_incidents("compute", node_type="alert")
    assert "inc-e" in alerts_only
    assert "inc-f" not in alerts_only


def test_find_related_incidents_empty_when_no_match(store):
    related = store.find_related_incidents("nonexistent-service")
    assert related == []


# ── Serialization round-trip ───────────────────────────────────────────────────

def test_node_serialization_round_trip(store):
    node = store.make_node(
        "change", "deploy-v2.3", "inc-rt",
        service="api", properties={"version": "2.3", "env": "prod"},
    )
    store.add_node(node)
    nodes = store.get_incident_nodes("inc-rt")
    assert len(nodes) == 1
    n = nodes[0]
    assert n.properties["version"] == "2.3"
    assert n.properties["env"] == "prod"
    d = n.to_dict()
    assert d["node_type"] == "change"
    assert d["label"] == "deploy-v2.3"


def test_edge_serialization_round_trip(store):
    edge = store.make_edge(
        "src", "tgt", "DEPENDS_ON", "inc-rt2",
        weight=0.75, properties={"reason": "runtime dependency"},
    )
    store.add_edge(edge)
    edges = store.get_incident_edges("inc-rt2")
    assert len(edges) == 1
    e = edges[0]
    assert e.weight == 0.75
    assert e.properties["reason"] == "runtime dependency"
    d = e.to_dict()
    assert d["relationship"] == "DEPENDS_ON"
