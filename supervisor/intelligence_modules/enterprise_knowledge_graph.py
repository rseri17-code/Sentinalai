"""EnterpriseKnowledgeGraph runner for the Intelligence Runtime.

Builds a runtime canonical Enterprise Knowledge Graph — a typed
entity+relationship graph — at POST_PERSIST from the accumulated
intelligence corpus of the current investigation.

The graph is pure runtime state. Nothing here writes to a database,
touches any existing store, or migrates any schema. The compact graph
summary is stashed on the receipt's ``intelligence`` metadata bag so
downstream consumers can key off it without reconstructing the graph.

Sources reused:
- IntelligenceContext (from ctx.phase_receipts) — the six read-module
  outputs assembled by ``sentinel_core.models.intel_context``.
- ctx.fetch_out.incident.incident_id + ctx.fetch_out.service — for the
  central incident and central service nodes.
- ctx.cres.incident_type — for the incident's classification.
- ctx.result.root_cause + ctx.result.remediation.immediate_action — for
  the current incident node's derived properties.

Feature-flag-gated: ``ENABLE_ENTERPRISE_KNOWLEDGE_GRAPH``. Default off.

Never raises. Runtime failure isolation catches internal errors.
"""
from __future__ import annotations

import logging
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.enterprise_knowledge_graph")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG = "ENABLE_ENTERPRISE_KNOWLEDGE_GRAPH"
GRAPH_VERSION = 1


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

ENTERPRISE_KNOWLEDGE_GRAPH_SPEC = ModuleSpec(
    name="enterprise_knowledge_graph",
    stage=IntelligenceStage.POST_PERSIST,
    feature_flag=ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG,
    # After resolution_memory (100), investigation_store (200) and the
    # intelligence_context_persister (800). The KG doesn't depend on
    # them for correctness (it reads from receipts), but it is
    # conceptually the last step of the persist stage.
    priority=900,
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def enterprise_knowledge_graph_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Build a per-investigation KnowledgeGraph from intelligence receipts.

    Returns receipt metadata containing the compact graph summary and
    node/edge counts.  The full graph dict is included for downstream
    observability but is bounded by the IntelligenceContext caps.

    Statuses:
        success — graph built (may be empty if no signals)
        skipped — no phase_receipts (early stage / disabled runtime path)
        failed  — runtime-captured error
    """
    receipts = ctx.phase_receipts or ()
    if not receipts:
        return {
            "status":  "skipped",
            "reason":  "no_phase_receipts",
            "version": GRAPH_VERSION,
        }

    # Lazy import to keep cycle risk contained
    from sentinel_core.models.intel_context import IntelligenceContext
    from sentinel_core.models.knowledge_graph import (
        KnowledgeGraph,
        KnowledgeGraphBuilder,
    )

    intel_ctx = IntelligenceContext.from_receipts(receipts)

    incident_id = _extract_incident_id(ctx)
    service = intel_ctx.service or _extract_service(ctx)
    incident_type = intel_ctx.incident_type or _extract_incident_type(ctx)
    root_cause, remediation = _extract_result_fields(ctx)

    graph: KnowledgeGraph = KnowledgeGraphBuilder.from_intelligence_context(
        intel_ctx,
        incident_id=incident_id,
        service=service,
        incident_type=incident_type,
        root_cause=root_cause,
        remediation_action=remediation,
    )

    # Node-type breakdown for compact receipt summary
    node_type_counts: dict[str, int] = {}
    for n in graph.nodes:
        node_type_counts[n.node_type] = node_type_counts.get(n.node_type, 0) + 1
    edge_type_counts: dict[str, int] = {}
    for e in graph.edges:
        edge_type_counts[e.edge_type] = edge_type_counts.get(e.edge_type, 0) + 1

    return {
        "status":            "success",
        "service":           service,
        "incident_id":       incident_id,
        "knowledge_graph":   graph.to_dict(),
        "graph_summary": {
            "node_count":       graph.node_count(),
            "edge_count":       graph.edge_count(),
            "node_type_counts": node_type_counts,
            "edge_type_counts": edge_type_counts,
        },
        "version":           GRAPH_VERSION,
    }


# ---------------------------------------------------------------------------
# Context extractors
# ---------------------------------------------------------------------------

def _extract_incident_id(ctx: RuntimeContext) -> str:
    if not (ctx.fetch_out and isinstance(ctx.fetch_out, dict)):
        return ""
    incident = ctx.fetch_out.get("incident")
    if isinstance(incident, dict):
        v = incident.get("incident_id") or ""
        if v:
            return str(v)
    return ""


def _extract_service(ctx: RuntimeContext) -> str:
    if ctx.fetch_out and isinstance(ctx.fetch_out, dict):
        v = ctx.fetch_out.get("service", "")
        if v:
            return str(v)
    return ""


def _extract_incident_type(ctx: RuntimeContext) -> str:
    if ctx.cres is not None:
        v = getattr(ctx.cres, "incident_type", "")
        if v:
            return str(v)
    return ""


def _extract_result_fields(ctx: RuntimeContext) -> tuple[str, str]:
    """Return (root_cause, immediate_action). Empty strings when absent."""
    result = ctx.result if isinstance(ctx.result, dict) else {}
    root_cause = str(result.get("root_cause", "") or "")
    remediation = result.get("remediation") or {}
    if isinstance(remediation, dict):
        action = str(remediation.get("immediate_action", "") or "")
    else:
        action = ""
    return root_cause, action


__all__ = [
    "ENTERPRISE_KNOWLEDGE_GRAPH_SPEC",
    "ENTERPRISE_KNOWLEDGE_GRAPH_FEATURE_FLAG",
    "GRAPH_VERSION",
    "enterprise_knowledge_graph_runner",
]
