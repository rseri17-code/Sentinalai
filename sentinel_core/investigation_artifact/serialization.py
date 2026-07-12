"""Serialization + deterministic identity for InvestigationArtifact.

- ``canonical_json``: sort_keys, compact separators — byte-identical for
  equal inputs (the platform-wide determinism convention).
- ``make_artifact_id``: sha256[:16] over framed canonical JSON (RC-G —
  never delimiter-joined strings).
- ``artifact_to_dict`` / ``artifact_from_dict``: JSON-safe round trip.
  ``from_dict`` preserves the stored ``schema_version`` verbatim (RC-I).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping

from sentinel_core.models._coerce import coerce_int, coerce_str
from sentinel_core.investigation_artifact.schemas import (
    ARTIFACT_SCHEMA_VERSION,
    InvestigationArtifact,
)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, str fallback."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# Fields excluded from the identity hash:
# - artifact_id      (would be self-referential)
# - admission_state  (lifecycle metadata; identity must survive transitions)
_ID_EXCLUDED_FIELDS = ("artifact_id", "admission_state")


def make_artifact_id(content: Mapping[str, Any]) -> str:
    """Deterministic artifact id over the artifact's content fields.

    RC-G: framed canonical JSON, never delimiter-joined strings. The same
    logical investigation (same receipts, result summaries and caller-
    supplied created_at) always produces the same id; any meaningful
    content change produces a different id.
    """
    body = {k: v for k, v in content.items() if k not in _ID_EXCLUDED_FIELDS}
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()[:16]


def _plain(obj: Any) -> Any:
    """Recursively convert to JSON-native types (RC-I: tuples → lists,
    frozen dicts → plain dicts)."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def artifact_to_dict(artifact: InvestigationArtifact) -> dict[str, Any]:
    """JSON-safe dict: every tuple a list, every frozen dict a plain dict.

    Satisfies ``d == json.loads(json.dumps(d))``.
    """
    return _plain(asdict(artifact))


def artifact_from_dict(d: Mapping[str, Any]) -> InvestigationArtifact:
    """Rebuild an artifact from a stored dict.

    Best-effort, RC-H coercion on scalars. RC-I: the stored
    ``schema_version`` is preserved verbatim, never rewritten.
    """
    def _dict(key: str) -> dict[str, Any]:
        v = d.get(key)
        return dict(v) if isinstance(v, dict) else {}

    receipts = d.get("phase_receipts") or ()
    hashes = d.get("receipt_hashes") or ()
    return InvestigationArtifact(
        artifact_id=coerce_str(d.get("artifact_id")),
        incident_id=coerce_str(d.get("incident_id")),
        investigation_id=coerce_str(d.get("investigation_id")),
        created_at=coerce_str(d.get("created_at")),
        root_cause=coerce_str(d.get("root_cause")),
        confidence=max(0, min(100, coerce_int(d.get("confidence")))),
        status=coerce_str(d.get("status"), "completed"),
        # B2 enrichment — additive; pre-enrichment artifacts default.
        service=coerce_str(d.get("service")),
        incident_type=coerce_str(d.get("incident_type")),
        severity=coerce_str(d.get("severity")),
        environment=coerce_str(d.get("environment")),
        application=coerce_str(d.get("application")),
        resolution=coerce_str(d.get("resolution")),
        false_leads=tuple(coerce_str(x) for x in (d.get("false_leads") or ())),
        runtime_cost=coerce_int(d.get("runtime_cost")),
        phase_receipts=tuple(r for r in receipts if isinstance(r, dict)),
        receipt_hashes=tuple(coerce_str(h) for h in hashes),
        final_result_summary=_dict("final_result_summary"),
        decision_summary=_dict("decision_summary"),
        evidence_key_summary=_dict("evidence_key_summary"),
        planner_trace_summary=_dict("planner_trace_summary"),
        worker_execution_summary=_dict("worker_execution_summary"),
        admission_state=coerce_str(d.get("admission_state"), "candidate"),
        provenance=_dict("provenance"),
        replay_pointer=coerce_str(d.get("replay_pointer")),
        benchmark_pointer=coerce_str(d.get("benchmark_pointer")),
        memory_pointer=coerce_str(d.get("memory_pointer")),
        schema_version=coerce_int(d.get("schema_version"),
                                  ARTIFACT_SCHEMA_VERSION),
    )


__all__ = [
    "artifact_from_dict",
    "artifact_to_dict",
    "canonical_json",
    "make_artifact_id",
]
