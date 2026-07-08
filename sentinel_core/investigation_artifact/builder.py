"""build_artifact — pure projection from (result, receipts) to the artifact.

Consumes the final result dict (with ``_phase_receipts`` attached by the
supervisor's ``attach_receipts``) and produces the canonical immutable
InvestigationArtifact. Follows the ``IntelligenceContext.from_receipts``
projection pattern: pure, deterministic, tolerant of missing fields.

Redaction (RC-A) is applied to every receipt dict and summary before the
artifact is materialised, so the artifact is safe to retain for years.

Never reads the filesystem. Never reads env. Never calls ``now()`` —
``created_at`` is caller-supplied.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

# RC-A redaction helpers — single implementation, shared with UIReceipt.
from sentinel_core.models.receipts import _redact_params  # noqa: PLC2701
from sentinel_core.models._coerce import coerce_int, coerce_str
from sentinel_core.investigation_artifact.schemas import InvestigationArtifact
from sentinel_core.investigation_artifact.serialization import (
    canonical_json,
    make_artifact_id,
)

# Keys of the result dict that are internal metadata, not investigation
# content. Listed in the final_result_summary but never copied wholesale.
_RESULT_META_PREFIX = "_"

# The five runtime phases; fewer finalized receipts ⇒ early return.
_FULL_PHASE_COUNT = 5

# Scalar result fields lifted into final_result_summary verbatim (safe,
# small, useful to admission and offline learning).
_SUMMARY_SCALARS = (
    "online_quality_score", "citation_coverage", "hallucination_risk",
    "confidence_degraded", "confidence_degraded_reason", "incident_id",
)


def _receipt_hash(receipt: Mapping[str, Any]) -> str:
    """sha256[:16] of the framed canonical JSON of one receipt (RC-G)."""
    return hashlib.sha256(canonical_json(receipt).encode()).hexdigest()[:16]


def _derive_status(root_cause: str, receipts: tuple) -> str:
    """Collapse run shape into one queryable OUTCOME_STATUSES value."""
    if root_cause == "META_QUERY_NOT_INCIDENT":
        return "meta_query"
    if root_cause.startswith("BLOCKED:"):
        return "blocked"
    if any(str(r.get("status", "")) == "failed" for r in receipts):
        return "failed"
    if len(receipts) < _FULL_PHASE_COUNT:
        return "early_return"
    return "completed"


def _final_result_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Compact projection: sorted top-level key names + safe scalars.

    Never copies large values — the full result lives with the caller and
    the replay store; the artifact records shape and quality signals.
    """
    summary: dict[str, Any] = {
        "keys": sorted(str(k) for k in result.keys()),
    }
    for k in _SUMMARY_SCALARS:
        if k in result:
            v = result[k]
            if isinstance(v, (str, int, float, bool)) or v is None:
                summary[k] = v
    return summary


def _decision_summary(receipts: tuple) -> dict[str, Any]:
    """Lift the decision_intelligence module payload from receipt metadata.

    The as-was snapshot of what the system believed mid-run — semantically
    distinct from any later re-derivation.
    """
    for r in receipts:
        meta = r.get("metadata")
        if not isinstance(meta, dict):
            continue
        for entry in meta.get("intelligence") or ():
            if isinstance(entry, dict) and \
                    str(entry.get("name", "")) == "decision_intelligence":
                payload = entry.get("payload") or entry.get("result") or {}
                return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _evidence_key_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Evidence keys + count from ``_evidence_snapshot`` (bool-per-key).

    Keys, counts and hashes only — never evidence values (contract rule).
    """
    snap = result.get("_evidence_snapshot")
    if not isinstance(snap, dict):
        return {"keys": [], "count": 0}
    keys = sorted(str(k) for k in snap.keys())
    return {"keys": keys, "count": len(keys)}


def _planner_trace_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Compact planner-trace projection when the trace is present.

    Step labels and count only — full rationale strings stay with the
    replay bundle.
    """
    trace = result.get("_planner_trace")
    if isinstance(trace, dict):
        steps = trace.get("steps") or ()
        labels = [
            f"{s.get('worker', '')}.{s.get('action', '')}"
            for s in steps if isinstance(s, dict)
        ]
        return {"steps": labels, "count": len(labels)}
    return {"steps": [], "count": 0}


def _worker_execution_summary(receipts: tuple) -> dict[str, Any]:
    """Per-phase status/elapsed/evidence-count projection from receipts.

    Worker-call-level detail lives in the UIReceipt store (linked via
    receipt references) — this is the phase-level roll-up.
    """
    phases: dict[str, Any] = {}
    for r in receipts:
        name = str(r.get("phase_name", ""))
        if not name:
            continue
        phases[name] = {
            "status": str(r.get("status", "")),
            "elapsed_ms": coerce_int(r.get("elapsed_ms")),
            "evidence_after": coerce_int(r.get("evidence_count_after")),
        }
    return phases


def build_artifact(
    result: Mapping[str, Any],
    incident_id: str,
    investigation_id: str = "",
    created_at: str = "",
    provenance: Mapping[str, Any] | None = None,
) -> InvestigationArtifact:
    """Build the canonical artifact from a completed investigation result.

    ``result`` must be the final dict returned by ``investigate()`` with
    ``_phase_receipts`` attached. Pure and deterministic: identical inputs
    produce a byte-identical artifact with the same ``artifact_id``.
    """
    raw_receipts = result.get("_phase_receipts") or ()
    receipts = tuple(
        _redact_params(r) for r in raw_receipts if isinstance(r, dict)
    )
    root_cause = coerce_str(result.get("root_cause"))
    confidence = max(0, min(100, coerce_int(result.get("confidence"))))

    content: dict[str, Any] = {
        "incident_id": coerce_str(incident_id),
        "investigation_id": coerce_str(investigation_id),
        "created_at": coerce_str(created_at),
        "root_cause": root_cause,
        "confidence": confidence,
        "status": _derive_status(root_cause, receipts),
        "phase_receipts": [dict(r) for r in receipts],
        "receipt_hashes": [_receipt_hash(r) for r in receipts],
        "final_result_summary": _final_result_summary(result),
        "decision_summary": _decision_summary(receipts),
        "evidence_key_summary": _evidence_key_summary(result),
        "planner_trace_summary": _planner_trace_summary(result),
        "worker_execution_summary": _worker_execution_summary(receipts),
        "provenance": _redact_params(dict(provenance or {})),
        "replay_pointer": coerce_str(incident_id),
        "benchmark_pointer": "",
        "memory_pointer": "",
        "schema_version": 1,
    }
    artifact_id = make_artifact_id(content)
    return InvestigationArtifact(
        artifact_id=artifact_id,
        incident_id=content["incident_id"],
        investigation_id=content["investigation_id"],
        created_at=content["created_at"],
        root_cause=root_cause,
        confidence=confidence,
        status=content["status"],
        phase_receipts=receipts,
        receipt_hashes=tuple(content["receipt_hashes"]),
        final_result_summary=content["final_result_summary"],
        decision_summary=content["decision_summary"],
        evidence_key_summary=content["evidence_key_summary"],
        planner_trace_summary=content["planner_trace_summary"],
        worker_execution_summary=content["worker_execution_summary"],
        provenance=content["provenance"],
        replay_pointer=content["replay_pointer"],
    )


__all__ = ["build_artifact"]
