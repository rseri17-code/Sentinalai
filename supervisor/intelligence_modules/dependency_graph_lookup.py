"""DependencyGraphLookup runner for the Intelligence Runtime.

Fourth read-path module. Runs at POST_CLASSIFY and consults the
persisted service dependency topology so investigate() can surface,
alongside the historical + pattern + related-incident signals:
- upstream services this incident's service depends on
- downstream services affected by this incident (blast radius)

Source queried (verbatim, no schema change):
- ``intelligence.dependency_graph.DependencyGraphStore`` — populated by
  ``intelligence.intel_writer._capture_dependencies`` on every completed
  investigation.

Never raises. Runtime failure isolation catches internal errors.

Feature-flag-gated: ``ENABLE_DEPENDENCY_GRAPH_LOOKUP``. Default off.
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

logger = logging.getLogger("sentinalai.intelligence_modules.dependency_graph_lookup")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG = "ENABLE_DEPENDENCY_GRAPH_LOOKUP"
LOOKUP_VERSION = 1

_MAX_EDGES_PER_DIRECTION = 20


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

DEPENDENCY_GRAPH_LOOKUP_SPEC = ModuleSpec(
    name="dependency_graph_lookup",
    stage=IntelligenceStage.POST_CLASSIFY,
    feature_flag=DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG,
    priority=400,                     # after historical / pattern / incident-graph
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def dependency_graph_lookup_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Report upstream + downstream service topology + blast radius.

    Returns:
        {status, service,
         upstream:          [{target_service, dep_type, strength, observed_count}],
         downstream:        [{source_service, dep_type, strength, observed_count}],
         affected_services: [str, ...],
         edge_counts:       {upstream, downstream, affected},
         version}

    Statuses:
        success — query succeeded; possibly empty edges
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

    upstream, downstream, affected = _query_all(service=service)

    return {
        "status":            "success",
        "service":           service,
        "upstream":          upstream[:_MAX_EDGES_PER_DIRECTION],
        "downstream":        downstream[:_MAX_EDGES_PER_DIRECTION],
        "affected_services": affected[:_MAX_EDGES_PER_DIRECTION],
        "edge_counts": {
            "upstream":   len(upstream[:_MAX_EDGES_PER_DIRECTION]),
            "downstream": len(downstream[:_MAX_EDGES_PER_DIRECTION]),
            "affected":   len(affected[:_MAX_EDGES_PER_DIRECTION]),
        },
        "version":           LOOKUP_VERSION,
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
# Store queries — each isolated; a failure in one direction doesn't blank
# the other.
# ---------------------------------------------------------------------------

def _query_all(*, service: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    return (
        _query_upstream(service=service),
        _query_downstream(service=service),
        _query_affected(service=service),
    )


def _make_edge_dict(dep) -> dict[str, Any]:
    return {
        "source_service": dep.source_service,
        "target_service": dep.target_service,
        "dep_type":       dep.dep_type,
        "strength":       round(dep.strength, 3),
        "observed_count": int(dep.observed_count),
        "last_seen":      str(dep.last_seen or ""),
    }


def _query_upstream(*, service: str) -> list[dict[str, Any]]:
    try:
        from intelligence.dependency_graph import DependencyGraphStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        return [_make_edge_dict(d) for d in DependencyGraphStore(db_path).get_upstream(service)]
    except Exception as exc:
        logger.debug("dependency_graph_lookup: upstream failed: %s", exc)
        return []


def _query_downstream(*, service: str) -> list[dict[str, Any]]:
    try:
        from intelligence.dependency_graph import DependencyGraphStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        return [_make_edge_dict(d) for d in DependencyGraphStore(db_path).get_downstream(service)]
    except Exception as exc:
        logger.debug("dependency_graph_lookup: downstream failed: %s", exc)
        return []


def _query_affected(*, service: str) -> list[str]:
    try:
        from intelligence.dependency_graph import DependencyGraphStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        return list(DependencyGraphStore(db_path).get_affected_services(service))
    except Exception as exc:
        logger.debug("dependency_graph_lookup: affected failed: %s", exc)
        return []


__all__ = [
    "DEPENDENCY_GRAPH_LOOKUP_SPEC",
    "DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG",
    "LOOKUP_VERSION",
    "dependency_graph_lookup_runner",
]
