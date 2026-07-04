"""IncidentGraphLookup runner for the Intelligence Runtime.

Third read-path module. Runs at POST_CLASSIFY alongside
``historical_lookup`` and ``pattern_recognition`` and consults the
cross-investigation ``IncidentGraphStore`` for other incidents that
share the current service — the entry point for Root Cause Navigator
and Impact Analysis.

Source queried (verbatim, no schema change):
- ``intelligence.incident_graph.IncidentGraphStore`` — populated by
  ``intelligence.intel_writer._capture_incident_graph`` on every
  completed investigation. Contains typed nodes (service | host | root_cause | outcome)
  and typed edges (CAUSED_BY | PRECEDED | CORRELATED | AFFECTS).

Two lookup calls:
- **find_related_incidents(service)** → incident_ids that touched the
  current service. This is the "we have seen incidents on this service
  before" signal — Root Cause Navigator entry.
- **get_incident_edges(incident_id)** for the *current* incident is
  intentionally NOT called from here — the store contains nothing for
  the current incident yet at POST_CLASSIFY (writes happen post-flight
  via intel_writer).

Results ride on ``ModuleResult.metadata`` and land under
``receipt.metadata["intelligence"]["incident_graph_lookup"]``. No
downstream consumer required. Off *and* on: pipeline is byte-identical
to today.

Feature-flag-gated: ``ENABLE_INCIDENT_GRAPH_LOOKUP``. Default off.

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

logger = logging.getLogger("sentinalai.intelligence_modules.incident_graph_lookup")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG = "ENABLE_INCIDENT_GRAPH_LOOKUP"
LOOKUP_VERSION = 1

_MAX_RELATED_INCIDENTS = 10


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

INCIDENT_GRAPH_LOOKUP_SPEC = ModuleSpec(
    name="incident_graph_lookup",
    stage=IntelligenceStage.POST_CLASSIFY,
    feature_flag=INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG,
    priority=300,                       # after historical_lookup(100), pattern_recognition(200)
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def incident_graph_lookup_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Consult IncidentGraphStore for prior incidents on the current service.

    Returns:
        {status, service, current_incident_id,
         related_incident_ids: [str, ...],
         related_incident_count: int,
         version}

    Statuses:
        success — query succeeded; matches (possibly empty) reported
        skipped — no service present in fetch_out
        failed  — runtime-captured error
    """
    service = _extract_service(ctx)
    current_incident_id = _extract_incident_id(ctx)

    if not service:
        return {
            "status":  "skipped",
            "reason":  "no_service",
            "version": LOOKUP_VERSION,
        }

    related = _query_related_incidents(service=service)
    # Filter out the current incident so we only surface *other* incidents
    # on this service.
    if current_incident_id:
        related = [i for i in related if i != current_incident_id]

    return {
        "status":                 "success",
        "service":                service,
        "current_incident_id":    current_incident_id,
        "related_incident_ids":   related[:_MAX_RELATED_INCIDENTS],
        "related_incident_count": len(related[:_MAX_RELATED_INCIDENTS]),
        "version":                LOOKUP_VERSION,
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


def _extract_incident_id(ctx: RuntimeContext) -> str:
    if not (ctx.fetch_out and isinstance(ctx.fetch_out, dict)):
        return ""
    incident = ctx.fetch_out.get("incident")
    if isinstance(incident, dict):
        v = incident.get("incident_id") or ""
        if v:
            return str(v)
    return ""


# ---------------------------------------------------------------------------
# Store query
# ---------------------------------------------------------------------------

def _query_related_incidents(*, service: str) -> list[str]:
    """Query IncidentGraphStore.find_related_incidents. Never raises."""
    try:
        from intelligence.incident_graph import IncidentGraphStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        return IncidentGraphStore(db_path).find_related_incidents(
            service=service,
            limit=_MAX_RELATED_INCIDENTS * 2,   # over-request; we filter the current id
        )
    except Exception as exc:
        logger.debug("incident_graph_lookup: query failed: %s", exc)
        return []


__all__ = [
    "INCIDENT_GRAPH_LOOKUP_SPEC",
    "INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG",
    "LOOKUP_VERSION",
    "incident_graph_lookup_runner",
]
