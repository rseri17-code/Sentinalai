"""MTTI instrumentation — Mean Time To Identify, per investigation.

Computes the decision-acceleration timeline from the investigation's OWN
recorded event stream (``AGUIEvent.timestamp_epoch_ms``). It reads existing
runtime telemetry — it adds no clock to the deterministic investigation core
and invents nothing: a milestone that was never emitted stays ``None`` and its
segment is omitted rather than guessed.

Milestones (mapped to the events the runtime bridge already emits):
  * started        → investigation.started
  * first_evidence → first tool.responded / memory.result (evidence in hand)
  * root_cause     → rca.generated (fallback hypothesis.selected)
  * owner          → available at rca.generated (owner is derived with the RCA)
  * recommendation → available at rca.generated (next action ships with the RCA)
  * completed      → investigation.completed

Segments answer the MTTI question: how long until the operator can *act*?
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

MTTI_SCHEMA_VERSION = 1

_EVIDENCE_EVENTS = ("tool.responded", "memory.result")
_ROOT_CAUSE_EVENTS = ("rca.generated", "hypothesis.selected")


def _event_type(e: Mapping[str, Any]) -> str:
    t = e.get("event_type", "")
    return getattr(t, "value", t) if not isinstance(t, str) else t


def _epoch_ms(e: Mapping[str, Any]) -> Optional[int]:
    v = e.get("timestamp_epoch_ms")
    return int(v) if isinstance(v, (int, float)) else None


def _first_ts(events: list[Mapping[str, Any]], types: tuple[str, ...]) -> Optional[int]:
    """Earliest timestamp among any of ``types`` (types are equivalent)."""
    stamps = [ms for e in events
              if _event_type(e) in types and (ms := _epoch_ms(e)) is not None]
    return min(stamps) if stamps else None


def _first_ts_priority(events: list[Mapping[str, Any]],
                       ordered_types: tuple[str, ...]) -> Optional[int]:
    """Earliest timestamp of the FIRST type (in priority order) that occurs.
    Used where one signal is authoritative and the other is only a fallback."""
    for t in ordered_types:
        ts = _first_ts(events, (t,))
        if ts is not None:
            return ts
    return None


def compute_mtti(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the MTTI timeline for one investigation's recorded events."""
    evs = [dict(e) for e in events]

    started = _first_ts(evs, ("investigation.started",))
    if started is None:
        # fall back to the earliest event of any kind
        all_ms = [ms for e in evs if (ms := _epoch_ms(e)) is not None]
        started = min(all_ms) if all_ms else None

    first_evidence = _first_ts(evs, _EVIDENCE_EVENTS)
    # rca.generated is authoritative; hypothesis.selected is only a fallback.
    root_cause = _first_ts_priority(evs, _ROOT_CAUSE_EVENTS)
    completed = _first_ts(evs, ("investigation.completed",))
    if completed is None:
        all_ms = [ms for e in evs if (ms := _epoch_ms(e)) is not None]
        completed = max(all_ms) if all_ms else None

    # owner + recommendation become available together with the RCA
    owner = root_cause
    recommendation = root_cause

    milestones = {
        "started": started,
        "first_evidence": first_evidence,
        "root_cause": root_cause,
        "owner": owner,
        "recommendation": recommendation,
        "completed": completed,
    }

    def seg(end: Optional[int]) -> Optional[int]:
        if started is None or end is None or end < started:
            return None
        return end - started

    segments = {
        "time_to_first_evidence_ms": seg(first_evidence),
        "time_to_root_cause_ms": seg(root_cause),
        "time_to_owner_ms": seg(owner),
        "time_to_recommendation_ms": seg(recommendation),
        "total_ms": seg(completed),
    }

    # The operator can act once root cause + recommendation are visible.
    actionable = root_cause is not None
    return {
        "schema_version": MTTI_SCHEMA_VERSION,
        "milestones": milestones,
        "segments_ms": segments,
        "actionable": actionable,
        "events_observed": len(evs),
        "note": ("Durations derive from recorded event timestamps; missing "
                 "milestones are null, never estimated."),
    }


def summarize_mtti(per_investigation: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate median MTTI segments across investigations (produce-only).

    Reports NOT_MEASURED semantics via null when there is no data — no baseline
    comparison is fabricated here; that requires a controlled pilot."""
    rows = list(per_investigation)

    def _median(key: str) -> Optional[float]:
        vals = sorted(r["segments_ms"][key] for r in rows
                      if isinstance(r.get("segments_ms", {}).get(key), (int, float)))
        if not vals:
            return None
        n = len(vals)
        mid = n // 2
        return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    keys = ("time_to_first_evidence_ms", "time_to_root_cause_ms",
            "time_to_owner_ms", "time_to_recommendation_ms", "total_ms")
    return {
        "investigations": len(rows),
        "median": {k: _median(k) for k in keys},
        "baseline_comparison": "NOT_MEASURED",   # requires a controlled pilot arm
    }


__all__ = ["MTTI_SCHEMA_VERSION", "compute_mtti", "summarize_mtti"]
