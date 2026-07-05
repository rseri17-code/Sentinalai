"""Cross-Incident Causal Graph — offline deterministic RCA intelligence layer.

Sits on top of Incident Intelligence Memory + Hypothesis Intelligence +
Strategy Optimizer. Never touches production runtime. No LLM.
"""
from __future__ import annotations

from sentinel_core.causal_graph.causal_edge import (
    CausalEdge,
    CausalEdgeType,
    make_edge_id,
)
from sentinel_core.causal_graph.causal_node import (
    CausalNode,
    CausalNodeType,
    make_node_id,
)
from sentinel_core.causal_graph.chain_detector import ChainDetector
from sentinel_core.causal_graph.graph_builder import CausalGraphBuilder
from sentinel_core.causal_graph.mtti_paths import MTTIPathRanker
from sentinel_core.causal_graph.rca_paths import RCAPathRanker
from sentinel_core.causal_graph.recommendation_engine import (
    CausalRecommendationEngine,
    CausalRecommendationKind,
)
from sentinel_core.causal_graph.recurrence import RecurrenceDetector
from sentinel_core.causal_graph.report import (
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
from sentinel_core.causal_graph.schemas import (
    CAUSAL_SCHEMA_VERSION,
    CausalChain,
    CausalGraph,
    CausalPath,
    CausalRecommendation,
    CausalRecurrence,
    MTTIPath,
    RCAPath,
)


__all__ = [
    "CAUSAL_SCHEMA_VERSION",
    # Node + edge
    "CausalNode",
    "CausalNodeType",
    "make_node_id",
    "CausalEdge",
    "CausalEdgeType",
    "make_edge_id",
    # Container types
    "CausalGraph",
    "CausalChain",
    "CausalPath",
    "CausalRecurrence",
    "RCAPath",
    "MTTIPath",
    "CausalRecommendation",
    "CausalRecommendationKind",
    # Engines
    "CausalGraphBuilder",
    "ChainDetector",
    "RecurrenceDetector",
    "RCAPathRanker",
    "MTTIPathRanker",
    "CausalRecommendationEngine",
    # Reports
    "render_causal_graph",
    "render_causal_chains",
    "render_rca_paths",
    "render_mtti_paths",
    "render_recurrence_report",
    "render_service_causal_profile",
    "render_causal_recommendations",
    "render_master_report",
    "to_json",
]
