"""HistoricalLookup runner for the Intelligence Runtime.

First READ-path module in the intelligence layer. Runs at POST_CLASSIFY
— after service + incident_type are known — and consults the
already-persisted intelligence corpus for prior investigations that
match the current shape.

Sources queried (verbatim, no schema change):
- ``intelligence.resolution_memory.ResolutionMemoryStore`` — durable
  resolutions from prior investigations (produced by the Phase 20
  ``resolution_memory`` runner).
- ``intelligence.investigation_store.InvestigationStore`` — EvidenceGraph
  envelopes from prior investigations (produced by the Phase 21
  ``investigation_store`` runner).

Results ride on ``ModuleResult.metadata`` and land under
``receipt.metadata["intelligence"]["historical_lookup"]``. No downstream
consumer is required — investigate() ignores the payload today, so with
the feature flag off *or* on, the pipeline is byte-identical. Future
intelligence modules (Pattern, Predictive, Guided Investigation) can
consume the receipt metadata without any further wiring.

Feature-flag-gated: ``ENABLE_HISTORICAL_LOOKUP``. Default off.

Never raises. Runtime failure isolation captures internal errors on the
ModuleResult and marks the run ``failed`` without affecting the rest of
the stage.
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

logger = logging.getLogger("sentinalai.intelligence_modules.historical_lookup")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HISTORICAL_LOOKUP_FEATURE_FLAG = "ENABLE_HISTORICAL_LOOKUP"
LOOKUP_VERSION = 1

# Per-source ceiling. Small on purpose — this is a hint surface, not a
# full history dump. Keeps receipt payload bounded.
_MAX_MATCHES_PER_SOURCE = 5

# Head truncation for root-cause blurbs on the ModuleResult so a long
# text doesn't bloat receipt payloads.
_ROOT_CAUSE_HEAD = 160


# ---------------------------------------------------------------------------
# ModuleSpec — declarative registration
# ---------------------------------------------------------------------------

HISTORICAL_LOOKUP_SPEC = ModuleSpec(
    name="historical_lookup",
    stage=IntelligenceStage.POST_CLASSIFY,
    feature_flag=HISTORICAL_LOOKUP_FEATURE_FLAG,
    priority=100,
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def historical_lookup_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Consult ResolutionMemory + InvestigationStore for prior matches.

    Runs at POST_CLASSIFY — the earliest stage at which both ``service``
    (from FetchPhase) and ``incident_type`` (from ClassificationPhase)
    are known. Returns a compact summary; the full records are not
    embedded to keep receipt payload bounded.

    Returns:
        {status, service, incident_type,
         resolution_memory_matches: [{memory_id, root_cause_head, confidence, recorded_at}],
         investigation_matches:     [{investigation_id, created_at, incident_type, service}],
         match_counts:              {resolution_memory, investigation},
         version}

    Statuses:
        success      — at least one source queried; matches (possibly empty) reported
        skipped      — insufficient signal (no service AND no incident_type)
        failed       — runtime-captured error (RuntimeContext missing data, etc.)
    """
    service = _extract_service(ctx)
    incident_type = _extract_incident_type(ctx)

    if not service and not incident_type:
        return {
            "status":  "skipped",
            "reason":  "no_service_and_no_incident_type",
            "version": LOOKUP_VERSION,
        }

    rm_matches = _query_resolution_memory(service=service, incident_type=incident_type)
    inv_matches = _query_investigation_store(service=service, incident_type=incident_type)

    return {
        "status":                    "success",
        "service":                   service,
        "incident_type":             incident_type,
        "resolution_memory_matches": rm_matches,
        "investigation_matches":     inv_matches,
        "match_counts": {
            "resolution_memory": len(rm_matches),
            "investigation":     len(inv_matches),
        },
        "version":                   LOOKUP_VERSION,
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


def _extract_incident_type(ctx: RuntimeContext) -> str:
    if ctx.cres is not None:
        v = getattr(ctx.cres, "incident_type", "")
        if v:
            return str(v)
    return ""


# ---------------------------------------------------------------------------
# Source-specific queries — each one isolated so a failure in one source
# does not blank out results from the other.
# ---------------------------------------------------------------------------

def _query_resolution_memory(*, service: str, incident_type: str) -> list[dict[str, Any]]:
    """Query ResolutionMemoryStore for prior resolutions. Never raises."""
    try:
        from intelligence.resolution_memory import ResolutionMemoryStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        rows = ResolutionMemoryStore(db_path).query(
            service=service or None,
            incident_type=incident_type or None,
            limit=_MAX_MATCHES_PER_SOURCE,
        )
    except Exception as exc:
        logger.debug("historical_lookup: RM query failed: %s", exc)
        return []
    return [
        {
            "memory_id":       m.memory_id,
            "root_cause_head": (m.detected_root_cause or "")[:_ROOT_CAUSE_HEAD],
            "confidence":      int(m.confidence or 0),
            "recorded_at":     str(m.recorded_at or ""),
            "service":         str(m.service or ""),
            "incident_type":   str(m.incident_type or ""),
        }
        for m in rows
    ]


def _query_investigation_store(*, service: str, incident_type: str) -> list[dict[str, Any]]:
    """Query InvestigationStore index for prior investigations. Never raises.

    Prefers a service match when present; falls back to incident_type.
    Both filters use the append-only index; no graph body is loaded.
    """
    try:
        from intelligence.investigation_store import InvestigationStore
        dir_path = os.environ.get("INVESTIGATIONS_DIR", "eval/investigations")
        store = InvestigationStore(investigations_dir=dir_path)
        records = []
        if service:
            records = store.find_by_service(service, last_n=_MAX_MATCHES_PER_SOURCE)
        if not records and incident_type:
            records = store.find_by_incident_type(incident_type, last_n=_MAX_MATCHES_PER_SOURCE)
    except Exception as exc:
        logger.debug("historical_lookup: IS query failed: %s", exc)
        return []
    return [
        {
            "investigation_id": r.investigation_id,
            "created_at":       str(r.created_at or ""),
            "incident_type":    str(r.incident_type or ""),
            "service":          str(r.service or ""),
        }
        for r in records[:_MAX_MATCHES_PER_SOURCE]
    ]


__all__ = [
    "HISTORICAL_LOOKUP_SPEC",
    "HISTORICAL_LOOKUP_FEATURE_FLAG",
    "LOOKUP_VERSION",
    "historical_lookup_runner",
]
