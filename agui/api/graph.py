"""Causal Graph API — service topology and blast radius endpoints."""
from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel

from intelligence.causal_graph import CausalGraph

logger = logging.getLogger("sentinalai.api.graph")

router = APIRouter(prefix="/api/graph", tags=["graph"])

# Module-level singleton — shared across all requests
_graph = CausalGraph()


class CoFailureRequest(BaseModel):
    source: str
    target: str
    propagation_ms: int = 0


@router.get("/topology")
def get_topology():
    """Return full service topology (nodes + edges)."""
    return _graph.get_topology()


@router.get("/blast-radius/{service_id}")
def get_blast_radius(service_id: str):
    """Return blast radius analysis from a given service."""
    result = _graph.get_blast_radius(service_id)
    return asdict(result)


@router.post("/co-failure")
def record_co_failure(body: CoFailureRequest):
    """Record an observed co-failure between two services."""
    _graph.record_co_failure(body.source, body.target, body.propagation_ms)
    return {"ok": True}


@router.get("/health-summary")
def get_health_summary():
    """Return health and alert count for all services."""
    topology = _graph.get_topology()
    return [
        {
            "service_id": n["service_id"],
            "health": n["health"],
            "alert_count": n["alert_count"],
        }
        for n in topology["nodes"]
    ]


@router.get("/learned-edges")
def get_learned_edges():
    """Return edges split into learned (observed_count > 0) vs seeded (observed_count == 0)."""
    try:
        topology = _graph.get_topology()
        edges = topology.get("edges", [])
        learned = [e for e in edges if e.get("observed_count", 0) > 0]
        seeded = [e for e in edges if e.get("observed_count", 0) == 0]
        total = len(edges)
        learning_rate = round(len(learned) / total, 4) if total > 0 else 0.0
        return {
            "total_edges": total,
            "learned_edges": learned,
            "seeded_edges": seeded,
            "learning_rate": learning_rate,
        }
    except Exception as exc:
        logger.warning("get_learned_edges failed: %s", exc)
        return {
            "total_edges": 0,
            "learned_edges": [],
            "seeded_edges": [],
            "learning_rate": 0.0,
        }
