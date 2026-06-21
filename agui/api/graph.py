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
