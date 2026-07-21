"""Operator-timeline telemetry — how long the OPERATOR needed (not the engine).

The engine timeline (`agui/mtti.py`) measures when the system produced evidence,
root cause, owner, recommendation. This module measures the *operator's* journey
through the product: when they opened the investigation, viewed evidence,
checked confidence and owner, and decided. The two are kept strictly separate
(Phase 6) — the gap between them is where product improvement lives.

It introduces **no new telemetry framework**: operator milestones are recorded
as ``pilot_telemetry`` ``operator_interaction`` events with the milestone in the
payload, and stored/loaded through the same append-only primitives. Timestamps
are supplied by the caller (the UI, at the real moment of interaction) — nothing
is synthesized, and a milestone never observed stays ``null``.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from sentinel_core.oip import pilot_telemetry as pt

OPERATOR_TELEMETRY_SCHEMA_VERSION = 1

# The operator milestones (Phase 2 vocabulary).
OPERATOR_MILESTONES = (
    "investigation_opened",
    "investigation_resumed",
    "evidence_panel_opened",
    "evidence_item_expanded",
    "timeline_opened",
    "graph_opened",
    "graph_node_selected",
    "confidence_viewed",
    "owner_viewed",
    "recommendation_viewed",
    "recommendation_accepted",
    "recommendation_rejected",
    "next_action_started",
    "investigation_completed",
    "external_tool_opened",
)


def operator_event(
    milestone: str,
    *,
    at: int,
    operator: str,
    investigation_id: str,
    service: str = "",
    application: str = "",
    entity: str = "",
    screen: str = "",
    duration_ms: Optional[int] = None,
    tool_name: str = "",
    reason: str = "",
    time_away_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Build one operator milestone event (reuses pilot_telemetry).

    ``at`` is a caller-supplied epoch-ms timestamp (the real interaction time).
    """
    if milestone not in OPERATOR_MILESTONES:
        raise ValueError(f"unknown operator milestone: {milestone!r}")
    payload = {
        "milestone": milestone,
        "at_ms": int(at),
        "service": service,
        "application": application,
        "entity": entity,
        "screen": screen,
        "duration_ms": duration_ms,
        "tool_name": tool_name,
        "reason": reason,
        "time_away_ms": time_away_ms,
    }
    # Reuse the existing recorder; caller-supplied ISO-ish 'at' as the event time.
    return pt.pilot_event(
        "operator_interaction",
        at=str(at),
        operator=operator,
        incident_id=investigation_id,
        payload=payload,
    )


def _milestone(e: Mapping[str, Any]) -> str:
    return str(e.get("payload", {}).get("milestone", ""))


def _at_ms(e: Mapping[str, Any]) -> Optional[int]:
    v = e.get("payload", {}).get("at_ms")
    return int(v) if isinstance(v, (int, float)) else None


def _first(events: list[Mapping[str, Any]], milestones: tuple[str, ...]) -> Optional[int]:
    stamps = [ms for e in events
              if _milestone(e) in milestones and (ms := _at_ms(e)) is not None]
    return min(stamps) if stamps else None


def _first_priority(events: list[Mapping[str, Any]],
                    ordered: tuple[str, ...]) -> Optional[int]:
    for m in ordered:
        ts = _first(events, (m,))
        if ts is not None:
            return ts
    return None


def compute_operator_mtti(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Operator-only timeline for one investigation. Never combined with system
    metrics; missing milestones are null (never estimated)."""
    evs = [dict(e) for e in events]

    opened = _first(evs, ("investigation_opened", "investigation_resumed"))
    first_evidence = _first_priority(
        evs, ("evidence_item_expanded", "evidence_panel_opened"))
    confidence = _first(evs, ("confidence_viewed",))
    owner = _first(evs, ("owner_viewed",))
    understanding = _first(evs, ("recommendation_viewed",))
    decision = _first(evs, ("recommendation_accepted", "recommendation_rejected"))
    next_action = _first(evs, ("next_action_started",))
    completed = _first(evs, ("investigation_completed",))

    def seg(end: Optional[int]) -> Optional[int]:
        if opened is None or end is None or end < opened:
            return None
        return end - opened

    return {
        "schema_version": OPERATOR_TELEMETRY_SCHEMA_VERSION,
        "milestones": {
            "opened": opened, "first_useful_evidence": first_evidence,
            "confidence": confidence, "owner": owner,
            "understanding": understanding, "decision": decision,
            "next_action": next_action, "completed": completed,
        },
        "operator_segments_ms": {
            "time_to_first_useful_evidence_ms": seg(first_evidence),
            "time_to_understanding_ms": seg(understanding),
            "time_to_confidence_ms": seg(confidence),
            "time_to_decision_ms": seg(decision),
            "time_to_next_action_ms": seg(next_action),
            "total_ms": seg(completed),
        },
        "events_observed": len(evs),
        "note": ("Operator timeline from recorded interaction timestamps; "
                 "kept separate from system MTTI; nulls are not estimated."),
    }


def external_tool_escapes(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Phase 3 — where operators left SentinelAI and for how long."""
    evs = [e for e in events if _milestone(e) == "external_tool_opened"]
    per_tool: dict[str, dict[str, Any]] = {}
    total_away = 0
    for e in evs:
        p = e.get("payload", {})
        tool = str(p.get("tool_name", "") or "(unknown)")
        away = p.get("time_away_ms")
        rec = per_tool.setdefault(tool, {"count": 0, "time_away_ms": 0,
                                         "reasons": []})
        rec["count"] += 1
        if isinstance(away, (int, float)):
            rec["time_away_ms"] += int(away)
            total_away += int(away)
        reason = str(p.get("reason", "") or "")
        if reason and reason not in rec["reasons"]:
            rec["reasons"].append(reason)
    return {"escapes": len(evs), "total_time_away_ms": total_away,
            "by_tool": {k: per_tool[k] for k in sorted(per_tool)}}


def decision_quality(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Phase 5 — accepted vs overridden. Every accept/reject is evidence."""
    evs = list(events)
    accepted = sum(1 for e in evs if _milestone(e) == "recommendation_accepted")
    rejected = sum(1 for e in evs if _milestone(e) == "recommendation_rejected")
    decided = accepted + rejected
    return {
        "recommendation_accepted": accepted,
        "recommendation_rejected": rejected,
        "acceptance_rate": round(accepted / decided, 4) if decided else None,
    }


def baseline_delta(baseline_ms: Optional[int],
                   sentinel_ms: Optional[int]) -> dict[str, Any]:
    """Phase 7 — seconds saved vs a baseline workflow. NOT_MEASURED without a
    real baseline; never fabricated."""
    if not isinstance(baseline_ms, (int, float)) or \
            not isinstance(sentinel_ms, (int, float)):
        return {"status": "NOT_MEASURED",
                "reason": "no controlled baseline arm supplied"}
    return {"status": "measured", "baseline_ms": int(baseline_ms),
            "sentinel_ms": int(sentinel_ms),
            "seconds_saved": round((baseline_ms - sentinel_ms) / 1000.0, 2)}


# storage reuse — same append-only primitives as pilot_telemetry
append_event = pt.append_event
load_events = pt.load_events


__all__ = [
    "OPERATOR_TELEMETRY_SCHEMA_VERSION", "OPERATOR_MILESTONES",
    "operator_event", "compute_operator_mtti", "external_tool_escapes",
    "decision_quality", "baseline_delta", "append_event", "load_events",
]
