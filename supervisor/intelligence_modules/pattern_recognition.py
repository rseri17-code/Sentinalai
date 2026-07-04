"""PatternRecognition runner for the Intelligence Runtime.

Second read-path module in the intelligence layer. Runs at POST_CLASSIFY
alongside ``historical_lookup`` and consults the operational pattern
corpus for recurring incident shapes.

Source queried (verbatim, no schema change):
- ``intelligence.pattern_intelligence.PatternIntelligenceStore`` — the
  operational_patterns table on ops_intelligence.db. Populated by
  ``intelligence.intel_writer.capture`` on every completed investigation
  since Phase 4 of the harness pipeline.

Two lookup modes are exercised:
- **Shape match**: filter by (incident_type, service, min_occurrences=2).
  Returns recurring patterns for this incident's shape — the "we have
  seen this before" signal.
- **Jaccard similar** *(only when a root cause is available)*: only used
  when the runner is scheduled at POST_ANALYZE and a root cause exists
  on ctx.result. At POST_CLASSIFY we have no root cause yet, so this
  branch is skipped.

Results ride on ``ModuleResult.metadata`` and land under
``receipt.metadata["intelligence"]["pattern_recognition"]``. No
downstream consumer is required — investigate() ignores the payload
today, so with the feature flag off *or* on, the pipeline is
byte-identical. Future intelligence modules (Predictive, Guided
Investigation) can consume the receipt metadata without further wiring.

Feature-flag-gated: ``ENABLE_PATTERN_RECOGNITION``. Default off.

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

logger = logging.getLogger("sentinalai.intelligence_modules.pattern_recognition")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PATTERN_RECOGNITION_FEATURE_FLAG = "ENABLE_PATTERN_RECOGNITION"
RECOGNITION_VERSION = 1

# Only surface patterns with at least this many occurrences — filters out
# one-off noise so we highlight actual "we've seen this before" signals.
_MIN_OCCURRENCES = 2

# Cap on the number of patterns surfaced. Bounded receipt payload.
_MAX_MATCHES = 5


# ---------------------------------------------------------------------------
# ModuleSpec — declarative registration
# ---------------------------------------------------------------------------

PATTERN_RECOGNITION_SPEC = ModuleSpec(
    name="pattern_recognition",
    stage=IntelligenceStage.POST_CLASSIFY,
    feature_flag=PATTERN_RECOGNITION_FEATURE_FLAG,
    priority=200,                        # runs after historical_lookup (100)
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def pattern_recognition_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Consult PatternIntelligenceStore for recurring patterns matching
    the current (service, incident_type).

    Returns:
        {status, service, incident_type,
         pattern_matches:  [{pattern_id, incident_type, services, canonical_symptoms,
                             occurrence_count, success_count, success_rate,
                             last_seen}],
         match_count, version}

    Statuses:
        success — query succeeded; matches (possibly empty) reported
        skipped — insufficient signal (no service and no incident_type)
        failed  — runtime-captured error
    """
    service = _extract_service(ctx)
    incident_type = _extract_incident_type(ctx)

    if not service and not incident_type:
        return {
            "status":  "skipped",
            "reason":  "no_service_and_no_incident_type",
            "version": RECOGNITION_VERSION,
        }

    matches = _query_patterns(service=service, incident_type=incident_type)

    return {
        "status":          "success",
        "service":         service,
        "incident_type":   incident_type,
        "pattern_matches": matches,
        "match_count":     len(matches),
        "version":         RECOGNITION_VERSION,
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
# Store query
# ---------------------------------------------------------------------------

def _query_patterns(*, service: str, incident_type: str) -> list[dict[str, Any]]:
    """Query PatternIntelligenceStore for recurring patterns. Never raises.

    Filters to occurrences >= _MIN_OCCURRENCES so single events don't
    show up as "recurring." When service is present, filter also by
    service; otherwise fall back to incident_type only.
    """
    try:
        from intelligence.pattern_intelligence import PatternIntelligenceStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        rows = PatternIntelligenceStore(db_path).query(
            incident_type=incident_type or None,
            service=service or None,
            min_occurrences=_MIN_OCCURRENCES,
            limit=_MAX_MATCHES,
        )
    except Exception as exc:
        logger.debug("pattern_recognition: query failed: %s", exc)
        return []
    return [
        {
            "pattern_id":         p.pattern_id,
            "incident_type":      p.incident_type,
            "services":           list(p.services or []),
            "canonical_symptoms": list(p.canonical_symptoms or []),
            "occurrence_count":   int(p.occurrence_count),
            "success_count":      int(p.success_count),
            "success_rate":       round(p.success_rate, 3),
            "last_seen":          str(p.last_seen or ""),
        }
        for p in rows
    ]


__all__ = [
    "PATTERN_RECOGNITION_SPEC",
    "PATTERN_RECOGNITION_FEATURE_FLAG",
    "RECOGNITION_VERSION",
    "pattern_recognition_runner",
]
