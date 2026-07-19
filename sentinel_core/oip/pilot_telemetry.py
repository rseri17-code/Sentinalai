"""OIP pilot instrumentation — operator-event recorder (produce-only).

Records the pilot events that have NO existing sink: operator interactions,
recommendation usage, and operator feedback. The other required measurements
are already emitted by the platform and are *reused*, not re-recorded:

  * investigation duration  → runtime ``ModuleResult.elapsed_ms`` + phase receipts
  * evidence access         → R2 ``_evidence_lifecycle`` counts
  * replay usage            → replay artifact + ``corpus_version``

This module adds no reasoning, no scoring, no new architecture. It is a thin,
deterministic, append-only event log: timestamps are supplied by the caller
(blocker-B2 discipline — no wall-clock read here), records are canonical JSON,
and each carries a content-addressed id. Imported by no runtime path; the
pilot harness / operator UI calls it out-of-band.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

PILOT_TELEMETRY_SCHEMA_VERSION = 1

# The operator-side event kinds this recorder owns (others are reused signals).
EVENT_KINDS = (
    "operator_interaction",   # opened/viewed an OIP surface
    "recommendation_usage",   # followed / dismissed a recommendation
    "operator_feedback",      # questionnaire response
)

# The five OIP surfaces an operator can interact with during the pilot.
OIP_SURFACES = (
    "operational_health",
    "incident_trends",
    "application_health",
    "service_reliability",
    "daily_operations_brief",
)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha16(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()[:16]


def pilot_event(
    kind: str,
    *,
    at: str,
    operator: str,
    surface: str = "",
    incident_id: str = "",
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one immutable pilot event record.

    ``at`` is a caller-supplied ISO timestamp (no wall-clock is read here).
    ``kind`` must be one of ``EVENT_KINDS``; ``surface`` (when set) must be a
    known OIP surface. Deterministic and JSON-safe.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown pilot event kind: {kind!r}")
    if surface and surface not in OIP_SURFACES:
        raise ValueError(f"unknown OIP surface: {surface!r}")

    body = {
        "schema_version": PILOT_TELEMETRY_SCHEMA_VERSION,
        "kind": kind,
        "at": str(at),
        "operator": str(operator),
        "surface": str(surface),
        "incident_id": str(incident_id),
        "payload": dict(payload or {}),
    }
    body["event_id"] = _sha16(body)
    return body


def append_event(path: str, event: Mapping[str, Any]) -> dict[str, Any]:
    """Append one event as a canonical JSON line to ``path`` (append-only)."""
    record = dict(event)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a") as f:
        f.write(_canonical(record) + "\n")
    return record


def load_events(path: str) -> list[dict[str, Any]]:
    """Read back all recorded events (deterministic file order)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def summarize(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Produce-only rollup for pilot reporting: counts by kind, by surface,
    and recommendation follow-through. No scoring, no inference."""
    by_kind: dict[str, int] = {}
    by_surface: dict[str, int] = {}
    followed = dismissed = 0
    for e in events:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        s = e.get("surface", "")
        if s:
            by_surface[s] = by_surface.get(s, 0) + 1
        if e["kind"] == "recommendation_usage":
            action = str(e.get("payload", {}).get("action", ""))
            if action == "followed":
                followed += 1
            elif action == "dismissed":
                dismissed += 1
    decided = followed + dismissed
    return {
        "events": len(events),
        "by_kind": by_kind,
        "by_surface": by_surface,
        "recommendation_followed": followed,
        "recommendation_dismissed": dismissed,
        "recommendation_acceptance_rate":
            round(followed / decided, 4) if decided else None,
    }


__all__ = [
    "PILOT_TELEMETRY_SCHEMA_VERSION", "EVENT_KINDS", "OIP_SURFACES",
    "pilot_event", "append_event", "load_events", "summarize",
]
