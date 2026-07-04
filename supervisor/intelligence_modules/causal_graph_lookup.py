"""CausalGraphLookup runner for the Intelligence Runtime.

Sixth read-path module. Runs at POST_CLASSIFY and consults the causal
topology graph for the *blast radius* of the current service — a BFS
from the failing service weighted by learned failure-correlation edges.

Source queried (verbatim, no schema change):
- ``intelligence.causal_graph.CausalGraph.get_blast_radius(service_id)``
  — returns propagation probabilities, propagation-latency estimates,
  severity assessment ("critical" | "high" | "medium" | "low"), and full
  affected-service list with paths.

This is the "explain causality" pillar of the North Star: given a
failure at service X, which downstream services are at risk and with
what probability?

Feature-flag-gated: ``ENABLE_CAUSAL_GRAPH_LOOKUP``. Default off.

Never raises. Runtime failure isolation catches internal errors.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.causal_graph_lookup")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG = "ENABLE_CAUSAL_GRAPH_LOOKUP"
LOOKUP_VERSION = 1

_MAX_AFFECTED = 20

_STORAGE_PATH_ENV = "CAUSAL_GRAPH_PATH"


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

CAUSAL_GRAPH_LOOKUP_SPEC = ModuleSpec(
    name="causal_graph_lookup",
    stage=IntelligenceStage.POST_CLASSIFY,
    feature_flag=CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG,
    priority=600,                     # last of the read modules
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def causal_graph_lookup_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Compute weighted blast radius for the current service.

    Returns:
        {status, service,
         severity:        "critical" | "high" | "medium" | "low",
         total_affected:  int,
         affected:        [{service_id, probability, propagation_ms, path}],
         version}

    Statuses:
        success — computation ran; empty affected list is valid
        skipped — no service present
        failed  — runtime-captured error
    """
    service = _extract_service(ctx)
    if not service:
        return {
            "status":  "skipped",
            "reason":  "no_service",
            "version": LOOKUP_VERSION,
        }

    blast = _compute_blast_radius(service=service)
    if blast is None:
        return {
            "status":         "success",
            "service":        service,
            "severity":       "low",
            "total_affected": 0,
            "affected":       [],
            "version":        LOOKUP_VERSION,
        }

    return {
        "status":         "success",
        "service":        service,
        "severity":       blast["severity"],
        "total_affected": blast["total_affected"],
        "affected":       blast["affected"][:_MAX_AFFECTED],
        "version":        LOOKUP_VERSION,
    }


# ---------------------------------------------------------------------------
# Context extractors
# ---------------------------------------------------------------------------

def _extract_service(ctx: RuntimeContext) -> str:
    if ctx.fetch_out and isinstance(ctx.fetch_out, dict):
        v = ctx.fetch_out.get("service", "")
        if v:
            return str(v)
    return ""


# ---------------------------------------------------------------------------
# Store query
# ---------------------------------------------------------------------------

def _compute_blast_radius(*, service: str) -> dict[str, Any] | None:
    """Invoke CausalGraph.get_blast_radius. Never raises."""
    try:
        from intelligence.causal_graph import CausalGraph
        path = os.environ.get(_STORAGE_PATH_ENV)
        graph = CausalGraph(storage_path=path) if path else CausalGraph()
        result = graph.get_blast_radius(service)
    except Exception as exc:
        logger.debug("causal_graph_lookup: blast radius failed: %s", exc)
        return None
    return {
        "severity":       str(result.severity or "low"),
        "total_affected": int(result.total_affected),
        "affected":       [
            {
                "service_id":    str(a.get("service_id", "")),
                "probability":   round(float(a.get("probability", 0.0)), 4),
                "propagation_ms": int(a.get("propagation_ms", 0)),
                "path":          list(a.get("path", [])),
            }
            for a in (result.affected or [])
        ],
    }


__all__ = [
    "CAUSAL_GRAPH_LOOKUP_SPEC",
    "CAUSAL_GRAPH_LOOKUP_FEATURE_FLAG",
    "LOOKUP_VERSION",
    "causal_graph_lookup_runner",
]
