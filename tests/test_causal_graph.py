"""Tests for intelligence/causal_graph.py and /api/graph/* endpoints.

All tests are hermetic — each creates an isolated temp file for the graph.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_graph(tmp_path_str: str):
    """Create a CausalGraph backed by a temp file (fresh, seeded)."""
    from intelligence.causal_graph import CausalGraph
    return CausalGraph(storage_path=os.path.join(tmp_path_str, "causal_graph.jsonl"))


@pytest.fixture
def tmp_graph(tmp_path):
    return _make_graph(str(tmp_path))


# ---------------------------------------------------------------------------
# Unit tests for CausalGraph
# ---------------------------------------------------------------------------

def test_seed_creates_nodes_and_edges(tmp_path):
    graph = _make_graph(str(tmp_path))
    topo = graph.get_topology()
    assert len(topo["nodes"]) >= 15
    assert len(topo["edges"]) >= 15


def test_get_topology_returns_nodes_and_edges(tmp_graph):
    topo = tmp_graph.get_topology()
    assert "nodes" in topo
    assert "edges" in topo
    assert isinstance(topo["nodes"], list)
    assert isinstance(topo["edges"], list)


def test_blast_radius_payment_service(tmp_graph):
    result = tmp_graph.get_blast_radius("payment-service")
    service_ids = [a["service_id"] for a in result.affected]
    assert "order-service" in service_ids
    assert "notification-service" in service_ids
    assert result.total_affected >= 2


def test_blast_radius_unknown_service(tmp_graph):
    result = tmp_graph.get_blast_radius("nonexistent-service")
    assert result.affected == []
    assert result.total_affected == 0
    assert result.severity == "low"


def test_record_co_failure_updates_correlation(tmp_path):
    graph = _make_graph(str(tmp_path))
    # payment-service -> order-service exists with correlation=0.75
    # After a co-failure, EMA should push it toward 1.0
    original = graph._edges[("payment-service", "order-service")].failure_correlation
    graph.record_co_failure("payment-service", "order-service", 100)
    updated = graph._edges[("payment-service", "order-service")].failure_correlation
    assert updated > original  # EMA pushes toward 1.0
    assert graph._edges[("payment-service", "order-service")].observed_count >= 1


def test_update_service_health(tmp_graph):
    tmp_graph.update_service_health("api-gateway", 0.5, 3)
    node = tmp_graph._nodes["api-gateway"]
    assert node.health == 0.5
    assert node.alert_count == 3


def test_blast_radius_severity_critical(tmp_graph):
    # api-gateway is tier-1 and has many downstream services
    result = tmp_graph.get_blast_radius("api-gateway")
    # tier-1 origin with 3+ affected should be critical
    assert result.severity == "critical"


def test_topology_node_has_required_fields(tmp_graph):
    topo = tmp_graph.get_topology()
    required = {"service_id", "display_name", "team", "tier", "health", "alert_count",
                "last_incident_ts", "technologies"}
    for node in topo["nodes"]:
        assert required.issubset(node.keys()), f"Missing fields in node: {node}"


def test_topology_edge_has_required_fields(tmp_graph):
    topo = tmp_graph.get_topology()
    required = {"source", "target", "edge_type", "call_volume", "failure_correlation",
                "avg_propagation_ms", "observed_count", "last_updated"}
    for edge in topo["edges"]:
        assert required.issubset(edge.keys()), f"Missing fields in edge: {edge}"


def test_graph_persists_and_reloads(tmp_path):
    path = os.path.join(str(tmp_path), "causal_graph.jsonl")
    from intelligence.causal_graph import CausalGraph

    g1 = CausalGraph(storage_path=path)
    g1.update_service_health("api-gateway", 0.42, 5)

    # Reload from disk
    g2 = CausalGraph(storage_path=path)
    node = g2._nodes["api-gateway"]
    assert abs(node.health - 0.42) < 0.001
    assert node.alert_count == 5


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

def _make_test_client(graph_instance):
    """Build a FastAPI TestClient with the graph singleton patched."""
    from fastapi import FastAPI
    import agui.api.graph as graph_module

    # Patch the module-level singleton
    original = graph_module._graph
    graph_module._graph = graph_instance
    try:
        # Build a minimal app with just the graph router
        app = FastAPI()
        app.include_router(graph_module.router)
        client = TestClient(app, raise_server_exceptions=True)
        return client, graph_module, original
    except Exception:
        graph_module._graph = original
        raise


def test_graph_api_topology_endpoint(tmp_graph):
    import agui.api.graph as graph_module
    original = graph_module._graph
    graph_module._graph = tmp_graph
    try:
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(graph_module.router)
        client = TestClient(app)
        resp = client.get("/api/graph/topology")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) >= 15
    finally:
        graph_module._graph = original


def test_graph_api_blast_radius_endpoint(tmp_graph):
    import agui.api.graph as graph_module
    original = graph_module._graph
    graph_module._graph = tmp_graph
    try:
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(graph_module.router)
        client = TestClient(app)
        resp = client.get("/api/graph/blast-radius/payment-service")
        assert resp.status_code == 200
        data = resp.json()
        assert data["origin_service"] == "payment-service"
        assert "affected" in data
        assert data["total_affected"] >= 2
    finally:
        graph_module._graph = original
