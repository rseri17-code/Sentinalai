"""Tests for intelligence/topology_learner.py.

Covers:
  - learn_from_evidence with blast_radius dict extracts services
  - learn_from_evidence with evidence_timeline list extracts services
  - Returns correct count of updates
  - Skips self-loops (primary_service → primary_service)
  - Handles empty/None evidence gracefully
  - Health update from evidence when error_rate present
  - learned-edges endpoint returns correct structure

All tests are standalone — no external I/O.  The CausalGraph is patched to
a temp-file-backed instance so nothing is written to eval/causal_graph.jsonl.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from intelligence.causal_graph import CausalGraph
from intelligence.topology_learner import TopologyLearner


# ---------------------------------------------------------------------------
# Helper: fresh in-memory CausalGraph (backed by a tmpfile, not seeded)
# ---------------------------------------------------------------------------

def _fresh_graph() -> CausalGraph:
    """Return a CausalGraph that writes to a throwaway temp file and is NOT seeded."""
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    # Bypass seed_demo_topology by pre-populating the file with one dummy node
    # so _load() finds something and skips auto-seeding.
    import json
    from dataclasses import asdict
    from intelligence.causal_graph import ServiceNode
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    dummy = ServiceNode("dummy-service", "Dummy", "test-team", 3, 1.0, 0, ts, [])
    with open(tmp.name, "w") as f:
        f.write(json.dumps({"_type": "node", "data": asdict(dummy)}) + "\n")
    return CausalGraph(storage_path=tmp.name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBlastRadiusExtraction:
    def test_blast_radius_dict_extracts_co_failing_services(self):
        """blast_radius field with affected_services list → edges recorded."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {
            "blast_radius": {
                "affected_services": [
                    {"service_id": "auth-service"},
                    {"service_id": "payment-api"},
                ]
            }
        }
        count = learner.learn_from_evidence("order-service", "latency", evidence)

        assert count >= 2
        edges = graph.get_topology()["edges"]
        targets = {e["target"] for e in edges if e["source"] == "order-service"}
        assert "auth-service" in targets
        assert "payment-api" in targets

    def test_cmdb_blast_radius_extracts_services(self):
        """cmdb_blast_radius field with affected_services list → edges recorded."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {
            "cmdb_blast_radius": {
                "affected_services": [
                    {"ci_name": "cache-service"},
                ]
            }
        }
        count = learner.learn_from_evidence("api-gateway", "cpu_spike", evidence)

        assert count >= 1
        edges = graph.get_topology()["edges"]
        targets = {e["target"] for e in edges if e["source"] == "api-gateway"}
        assert "cache-service" in targets


class TestEvidenceTimelineExtraction:
    def test_evidence_timeline_list_extracts_services(self):
        """evidence_timeline list with service fields → edges recorded."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {
            "evidence_timeline": [
                {"service": "worker-service", "event": "high error rate"},
                {"service": "db-writer", "event": "connection refused"},
            ]
        }
        count = learner.learn_from_evidence("api-service", "error_rate", evidence)

        assert count >= 2
        edges = graph.get_topology()["edges"]
        targets = {e["target"] for e in edges if e["source"] == "api-service"}
        assert "worker-service" in targets
        assert "db-writer" in targets


class TestUpdateCount:
    def test_returns_correct_count_of_updates(self):
        """count equals number of discovered edges + health update if present."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {
            "health_score": 0.4,
            "blast_radius": {
                "affected_services": [
                    {"service_id": "cache-service"},
                    {"service_id": "queue-worker"},
                ]
            },
        }
        count = learner.learn_from_evidence("payment-service", "latency", evidence)
        # 1 health update + 2 edge updates = 3
        assert count == 3

    def test_returns_zero_for_no_relevant_fields(self):
        """Evidence with no recognized fields → 0 updates."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {"root_cause": "memory leak", "confidence": 85}
        count = learner.learn_from_evidence("my-service", "oom", evidence)
        assert count == 0


class TestSelfLoopPrevention:
    def test_skips_self_loops(self):
        """primary_service should never appear as its own target."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {
            "blast_radius": {
                "affected_services": [
                    {"service_id": "payment-service"},   # same as primary
                    {"service_id": "auth-service"},
                ]
            }
        }
        learner.learn_from_evidence("payment-service", "latency", evidence)

        edges = graph.get_topology()["edges"]
        self_loops = [
            e for e in edges
            if e["source"] == "payment-service" and e["target"] == "payment-service"
        ]
        assert self_loops == []


class TestEmptyEvidence:
    def test_handles_none_evidence_gracefully(self):
        """None evidence → returns 0, does not raise."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)
        count = learner.learn_from_evidence("some-service", "latency", None)
        assert count == 0

    def test_handles_empty_dict_evidence_gracefully(self):
        """Empty dict evidence → returns 0, does not raise."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)
        count = learner.learn_from_evidence("some-service", "latency", {})
        assert count == 0

    def test_handles_empty_blast_radius_dict(self):
        """blast_radius present but empty → returns 0 edges."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)
        evidence = {"blast_radius": {}}
        count = learner.learn_from_evidence("some-service", "latency", evidence)
        assert count == 0


class TestHealthUpdate:
    def test_health_update_from_error_rate(self):
        """error_rate in evidence triggers health update for primary service."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {"error_rate": 0.35}
        count = learner.learn_from_evidence("payment-service", "error_rate", evidence)

        assert count >= 1
        nodes = {n["service_id"]: n for n in graph.get_topology()["nodes"]}
        assert "payment-service" in nodes
        # health = 1 - 0.35 = 0.65
        assert abs(nodes["payment-service"]["health"] - 0.65) < 0.01

    def test_health_update_from_health_score(self):
        """health_score in evidence sets health directly."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {"health_score": 0.2}
        count = learner.learn_from_evidence("auth-service", "latency", evidence)

        assert count >= 1
        nodes = {n["service_id"]: n for n in graph.get_topology()["nodes"]}
        assert "auth-service" in nodes
        assert abs(nodes["auth-service"]["health"] - 0.2) < 0.01

    def test_health_update_from_slo_status_breach(self):
        """slo_status='breach' → health set to 0.4."""
        graph = _fresh_graph()
        learner = TopologyLearner(graph)

        evidence = {"slo_status": "slo_breach"}
        learner.learn_from_evidence("order-service", "slo", evidence)

        nodes = {n["service_id"]: n for n in graph.get_topology()["nodes"]}
        assert "order-service" in nodes
        assert nodes["order-service"]["health"] == 0.4


class TestLearnedEdgesEndpoint:
    def test_learned_edges_endpoint_structure(self):
        """GET /api/graph/learned-edges returns correct keys and types."""
        import sys
        import os
        # Ensure the worktree root is on the path
        worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if worktree not in sys.path:
            sys.path.insert(0, worktree)

        from fastapi.testclient import TestClient
        from agui.api.graph import router, _graph
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/graph/learned-edges")
        assert resp.status_code == 200
        body = resp.json()
        assert "total_edges" in body
        assert "learned_edges" in body
        assert "seeded_edges" in body
        assert "learning_rate" in body
        assert isinstance(body["total_edges"], int)
        assert isinstance(body["learned_edges"], list)
        assert isinstance(body["seeded_edges"], list)
        assert isinstance(body["learning_rate"], float)

    def test_learned_edges_counts_add_up(self):
        """learned + seeded == total_edges."""
        import sys
        import os
        worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if worktree not in sys.path:
            sys.path.insert(0, worktree)

        from fastapi.testclient import TestClient
        from agui.api.graph import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/graph/learned-edges")
        body = resp.json()
        assert body["total_edges"] == len(body["learned_edges"]) + len(body["seeded_edges"])

    def test_seeded_edges_have_observed_count_zero(self):
        """All edges in seeded_edges list have observed_count == 0."""
        import sys
        import os
        worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if worktree not in sys.path:
            sys.path.insert(0, worktree)

        from fastapi.testclient import TestClient
        from agui.api.graph import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/graph/learned-edges")
        body = resp.json()
        for edge in body["seeded_edges"]:
            assert edge["observed_count"] == 0

    def test_learned_edges_have_observed_count_positive(self):
        """All edges in learned_edges list have observed_count > 0."""
        import sys
        import os
        worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if worktree not in sys.path:
            sys.path.insert(0, worktree)

        from fastapi.testclient import TestClient
        from agui.api.graph import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # Record a co-failure to create a learned edge
        resp_post = client.post(
            "/api/graph/co-failure",
            json={"source": "test-svc-a", "target": "test-api-b", "propagation_ms": 100},
        )
        assert resp_post.status_code == 200

        resp = client.get("/api/graph/learned-edges")
        body = resp.json()
        for edge in body["learned_edges"]:
            assert edge["observed_count"] > 0
