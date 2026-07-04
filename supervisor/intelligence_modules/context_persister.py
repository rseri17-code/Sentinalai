"""IntelligenceContextPersister runner for the Intelligence Runtime.

First **write consumer** of the intelligence read fan-out. Runs at
POST_PERSIST — after ResolutionMemory (100) and InvestigationStore
(200) — and writes a canonical per-investigation intelligence snapshot
to ``{INVESTIGATIONS_DIR}/{investigation_id}_intelligence.json``.

The snapshot is built from ``ctx.phase_receipts`` (populated by the
supervisor.agent ``_intel_hook`` at POST_PERSIST) via
``sentinel_core.models.intel_context.IntelligenceContext.from_receipts``
so no store re-query happens here.

The artifact is a JSON document with the fields specified in the
mission: investigation_id, incident_id, service, incident_type,
intelligence_modules_present, historical_matches_summary,
pattern_summary, graph_summary, causal_summary, episodic_summary,
confidence_signals, warnings, source_phase_names, generated_at,
schema_version.

Feature-flag-gated: ``ENABLE_INTELLIGENCE_CONTEXT_PERSIST``. Default off.

Never raises. Runtime failure isolation catches internal errors.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.context_persister")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG = "ENABLE_INTELLIGENCE_CONTEXT_PERSIST"
ARTIFACT_VERSION = 1

# Bounds: keep the artifact compact. Numbers picked to match the read modules'
# own caps so the artifact never grows beyond what the corpus can produce.
_MAX_WARNINGS = 20


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

INTELLIGENCE_CONTEXT_PERSIST_SPEC = ModuleSpec(
    name="intelligence_context_persister",
    stage=IntelligenceStage.POST_PERSIST,
    feature_flag=INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG,
    # After resolution_memory (100) and investigation_store (200) so their
    # cross-refs are in place, and after any other write module.
    priority=800,
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def intelligence_context_persister_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Build IntelligenceContext + persist as a per-investigation JSON.

    Returns:
        {status, artifact_path, module_names_present, total_signal_count,
         schema_version, version}

    Statuses:
        success       — artifact written (or already existed and matched)
        deduplicated  — artifact file already exists for this investigation_id
        skipped       — no phase_receipts on ctx (early stage / disabled path)
        failed        — runtime-captured error (I/O, serialization, etc.)
    """
    receipts = ctx.phase_receipts or ()

    # Skip semantics — nothing to package. This is different from
    # "empty intelligence" which is a valid artifact.
    if not receipts:
        return {
            "status":  "skipped",
            "reason":  "no_phase_receipts",
            "version": ARTIFACT_VERSION,
        }

    from sentinel_core.models.intel_context import IntelligenceContext

    intel_ctx = IntelligenceContext.from_receipts(receipts)

    # Investigation-level identity (not part of IntelligenceContext itself)
    incident_id = _extract_incident_id(ctx)
    service = intel_ctx.service or _extract_service(ctx)
    incident_type = intel_ctx.incident_type or _extract_incident_type(ctx)

    warnings = _collect_warnings(receipts)
    confidence_signals = _derive_confidence_signals(intel_ctx)
    source_phase_names = _collect_phase_names(receipts)

    artifact = {
        "schema_version":              ARTIFACT_VERSION,
        "generated_at":                _now_iso(),
        "investigation_id":            ctx.investigation_id,
        "incident_id":                 incident_id,
        "service":                     service,
        "incident_type":               incident_type,
        "intelligence_modules_present": list(intel_ctx.module_names_seen),
        "historical_matches_summary":  {
            "resolution_memory_matches": [asdictish(m) for m in intel_ctx.resolution_memory_matches],
            "investigation_matches":     [asdictish(m) for m in intel_ctx.investigation_matches],
        },
        "pattern_summary": [asdictish(p) for p in intel_ctx.pattern_matches],
        "graph_summary": {
            "related_incident_ids": list(intel_ctx.related_incident_ids),
            "upstream":             [asdictish(e) for e in intel_ctx.upstream_dependencies],
            "downstream":           [asdictish(e) for e in intel_ctx.downstream_dependents],
            "affected_services":    list(intel_ctx.affected_services),
        },
        "causal_summary": {
            "severity":       intel_ctx.blast_radius_severity,
            "total_affected": intel_ctx.blast_radius_total_affected,
            "affected":       [asdictish(a) for a in intel_ctx.blast_radius_affected],
        },
        "episodic_summary": [asdictish(e) for e in intel_ctx.episode_matches],
        "confidence_signals": confidence_signals,
        "warnings": warnings[:_MAX_WARNINGS],
        "source_phase_names": source_phase_names,
    }

    dir_path = os.environ.get("INVESTIGATIONS_DIR", "eval/investigations")
    artifact_path = os.path.join(dir_path, f"{ctx.investigation_id}_intelligence.json")

    # Dedup — if the artifact already exists for this investigation_id we
    # do not overwrite. This is safe because the artifact is derived from
    # receipts that are themselves derived from stores that persist by
    # investigation_id. Idempotent by design.
    if os.path.exists(artifact_path):
        return {
            "status":               "deduplicated",
            "artifact_path":        artifact_path,
            "module_names_present": list(intel_ctx.module_names_seen),
            "total_signal_count":   intel_ctx.total_signal_count(),
            "schema_version":       ARTIFACT_VERSION,
            "version":              ARTIFACT_VERSION,
        }

    try:
        os.makedirs(dir_path, exist_ok=True)
        tmp = artifact_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            # sort_keys makes the artifact byte-deterministic for the same
            # inputs, which matters for downstream diffing / replay checks.
            json.dump(artifact, f, indent=2, sort_keys=True)
        os.replace(tmp, artifact_path)
    except OSError as exc:
        # Let the runtime capture as "failed" so the receipt records the error.
        raise exc

    return {
        "status":               "success",
        "artifact_path":        artifact_path,
        "module_names_present": list(intel_ctx.module_names_seen),
        "total_signal_count":   intel_ctx.total_signal_count(),
        "schema_version":       ARTIFACT_VERSION,
        "version":              ARTIFACT_VERSION,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def asdictish(obj: Any) -> dict[str, Any]:
    """Convert a frozen dataclass to a plain dict. Tolerates non-dataclass."""
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(obj):
            return asdict(obj)
    except Exception:
        pass
    if isinstance(obj, dict):
        return dict(obj)
    return {}


def _extract_incident_id(ctx: RuntimeContext) -> str:
    fetch_out = ctx.fetch_out if isinstance(ctx.fetch_out, dict) else {}
    incident = fetch_out.get("incident") if isinstance(fetch_out, dict) else None
    if isinstance(incident, dict):
        v = incident.get("incident_id") or ""
        if v:
            return str(v)
    return ""


def _extract_service(ctx: RuntimeContext) -> str:
    fetch_out = ctx.fetch_out if isinstance(ctx.fetch_out, dict) else {}
    v = fetch_out.get("service", "") if isinstance(fetch_out, dict) else ""
    return str(v) if v else ""


def _extract_incident_type(ctx: RuntimeContext) -> str:
    if ctx.cres is not None:
        v = getattr(ctx.cres, "incident_type", "")
        if v:
            return str(v)
    return ""


def _collect_warnings(receipts) -> list[str]:
    """Aggregate warnings across all intelligence entries in the receipts.

    Malformed module metadata is isolated: any entry that isn't a dict or
    lacks required keys is silently skipped. We never crash on bad input.
    """
    out: list[str] = []
    for r in receipts or ():
        if not isinstance(r, dict):
            continue
        meta = r.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        entries = meta.get("intelligence")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for w in entry.get("warnings", []) or []:
                out.append(str(w))
    return out


def _collect_phase_names(receipts) -> list[str]:
    names: list[str] = []
    for r in receipts or ():
        if not isinstance(r, dict):
            continue
        name = r.get("phase_name") or r.get("name")
        if name:
            names.append(str(name))
    return names


def _derive_confidence_signals(intel_ctx) -> dict[str, Any]:
    """Compact derived-signal summary. Deterministic from IntelligenceContext.

    - top_resolution_memory_confidence: max confidence across RM matches (0 if none)
    - top_pattern_success_rate:         max success_rate across pattern matches
    - has_recurring_pattern:            true if any pattern has occurrence_count >= 2
    - blast_radius_severity:            copied from IntelligenceContext
    - has_related_incidents:            true if related_incident_ids non-empty
    """
    top_rm = 0
    for m in intel_ctx.resolution_memory_matches:
        if m.confidence > top_rm:
            top_rm = m.confidence

    top_pattern_rate = 0.0
    has_recurring = False
    for p in intel_ctx.pattern_matches:
        if p.success_rate > top_pattern_rate:
            top_pattern_rate = p.success_rate
        if p.occurrence_count >= 2:
            has_recurring = True

    return {
        "top_resolution_memory_confidence": int(top_rm),
        "top_pattern_success_rate":         round(float(top_pattern_rate), 3),
        "has_recurring_pattern":            bool(has_recurring),
        "blast_radius_severity":            intel_ctx.blast_radius_severity,
        "has_related_incidents":            bool(intel_ctx.related_incident_ids),
    }


__all__ = [
    "INTELLIGENCE_CONTEXT_PERSIST_SPEC",
    "INTELLIGENCE_CONTEXT_PERSIST_FEATURE_FLAG",
    "ARTIFACT_VERSION",
    "intelligence_context_persister_runner",
]
