"""Operational Improvement Engine — pilot telemetry → ranked improvement backlog.

Consumes existing artifacts only (operator events, operator/engine MTTI,
external-tool escapes, decision quality) and produces an evidence-backed,
ROI-ranked backlog of improvements that would reduce operator MTTI. It modifies
nothing and invents nothing: every backlog item traces to an observed signal,
and when there is not enough real data the whole report is ``NOT_MEASURED``
(Phase 1). ROI is grounded in *measured* cost (frequency × observed time),
never in invented weights — the only declared input is a coarse per-improvement
effort tier, surfaced separately, never used to manufacture impact.

Deterministic, produce-only. No clock, no randomness.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from agui.operator_telemetry import (
    compute_operator_mtti,
    decision_quality,
    external_tool_escapes,
)

IMPROVEMENT_ENGINE_SCHEMA_VERSION = 1

# Below this many sessions there is nothing to analyse -> NOT_MEASURED.
_MIN_SESSIONS = 5
# Below this, findings are provisional (underpowered) even if produced.
_POWERED_SESSIONS = 30

# Friction root-cause classes (Phase 3) and the coarse effort tier of the
# improvement that addresses each. Effort is declared, never observed; it is
# reported alongside impact, never multiplied into it.
_FRICTION_FIX = {
    "missing_evidence": {"improvement": "surface the missing evidence in-product",
                         "effort": "medium"},
    "poor_navigation": {"improvement": "reduce clicks / repeated lookups for this step",
                        "effort": "low"},
    "poor_visualization": {"improvement": "improve how this evidence is presented",
                           "effort": "medium"},
    "low_confidence": {"improvement": "expose confidence provenance sooner",
                       "effort": "low"},
    "missing_ownership": {"improvement": "surface owner earlier in the flow",
                          "effort": "low"},
    "missing_recommendation": {"improvement": "improve recommendation quality/trust",
                               "effort": "high"},
    "missing_context": {"improvement": "add the missing context to the RCA view",
                        "effort": "medium"},
}


def _median(vals: list[float]) -> Optional[float]:
    v = sorted(x for x in vals if isinstance(x, (int, float)))
    if not v:
        return None
    n = len(v)
    m = n // 2
    return float(v[m]) if n % 2 else (v[m - 1] + v[m]) / 2.0


def _group_by_investigation(events: Iterable[Mapping[str, Any]]) -> dict[str, list]:
    groups: dict[str, list] = {}
    for e in events:
        iid = str(e.get("incident_id", ""))
        if iid:
            groups.setdefault(iid, []).append(e)
    return groups


def analyze(operator_events: Iterable[Mapping[str, Any]],
            *, min_sessions: int = _MIN_SESSIONS) -> dict[str, Any]:
    """Produce the improvement report from recorded operator events."""
    events = list(operator_events)
    sessions = _group_by_investigation(events)
    n = len(sessions)

    if n < min_sessions:
        return {
            "schema_version": IMPROVEMENT_ENGINE_SCHEMA_VERSION,
            "status": "NOT_MEASURED",
            "reason": f"insufficient pilot data: {n} session(s) < {min_sessions}",
            "sessions": n,
        }

    per_session = {iid: compute_operator_mtti(evs) for iid, evs in sessions.items()}

    # --- Phase 1/2: friction per operator segment (median across sessions) ---
    seg_keys = ("time_to_first_useful_evidence_ms", "time_to_understanding_ms",
                "time_to_confidence_ms", "time_to_decision_ms",
                "time_to_next_action_ms", "total_ms")
    seg_median = {
        k: _median([m["operator_segments_ms"][k] for m in per_session.values()])
        for k in seg_keys
    }

    # --- escapes + decisions across the whole pilot ---
    escapes = external_tool_escapes(events)
    decisions = decision_quality(events)

    # repeated evidence lookups (a navigation/visualization signal)
    repeated_evidence = sum(
        max(0, sum(1 for e in evs
                   if e.get("payload", {}).get("milestone")
                   in ("evidence_panel_opened", "evidence_item_expanded")) - 1)
        for evs in sessions.values())

    bottlenecks = _bottlenecks(seg_median, escapes, repeated_evidence,
                               decisions, n)
    backlog = _rank(bottlenecks)

    return {
        "schema_version": IMPROVEMENT_ENGINE_SCHEMA_VERSION,
        "status": "measured",
        "sessions": n,
        "underpowered": n < _POWERED_SESSIONS,
        "operator_segment_median_ms": seg_median,
        "external_tool_escapes": escapes,
        "decision_quality": decisions,
        "repeated_evidence_lookups": repeated_evidence,
        "bottlenecks": bottlenecks,
        "backlog": backlog,
    }


def _bottlenecks(seg_median, escapes, repeated_evidence, decisions,
                 sessions) -> list[dict[str, Any]]:
    """Each bottleneck is derived from an observed signal and classified
    (Phase 3). seconds_saveable = observed frequency × observed time cost."""
    out: list[dict[str, Any]] = []

    # escapes -> missing evidence/context; saveable = total time away
    for tool, rec in escapes.get("by_tool", {}).items():
        away_s = round(rec["time_away_ms"] / 1000.0, 1)
        out.append({
            "signal": f"external_tool_escape:{tool}",
            "root_cause": "missing_evidence",
            "evidence": {"tool": tool, "count": rec["count"],
                         "total_time_away_ms": rec["time_away_ms"],
                         "reasons": rec["reasons"]},
            "frequency": rec["count"],
            "seconds_saveable": away_s,
        })

    # repeated evidence lookups -> navigation friction; ~ each repeat costs the
    # median first-evidence time again (observed proxy, not invented)
    ev_cost = seg_median.get("time_to_first_useful_evidence_ms")
    if repeated_evidence > 0 and isinstance(ev_cost, (int, float)):
        out.append({
            "signal": "repeated_evidence_lookups",
            "root_cause": "poor_navigation",
            "evidence": {"repeats": repeated_evidence,
                         "median_first_evidence_ms": ev_cost},
            "frequency": repeated_evidence,
            "seconds_saveable": round(repeated_evidence * ev_cost / 1000.0, 1),
        })

    # slow understanding -> missing context (the biggest single operator segment)
    und = seg_median.get("time_to_understanding_ms")
    conf = seg_median.get("time_to_confidence_ms")
    if isinstance(und, (int, float)) and isinstance(conf, (int, float)) and und > conf:
        out.append({
            "signal": "slow_time_to_understanding",
            "root_cause": "missing_context",
            "evidence": {"median_understanding_ms": und,
                         "median_confidence_ms": conf},
            "frequency": sessions,
            "seconds_saveable": round(sessions * (und - conf) / 1000.0, 1),
        })

    # low recommendation acceptance -> trust/recommendation friction
    rate = decisions.get("acceptance_rate")
    decided = (decisions.get("recommendation_accepted", 0)
               + decisions.get("recommendation_rejected", 0))
    if isinstance(rate, (int, float)) and rate < 0.5 and decided > 0:
        out.append({
            "signal": "low_recommendation_acceptance",
            "root_cause": "missing_recommendation",
            "evidence": {"acceptance_rate": rate,
                         "rejected": decisions.get("recommendation_rejected", 0)},
            "frequency": decided,
            "seconds_saveable": None,   # trust cost is not a time delta; not faked
        })

    return out


def _rank(bottlenecks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by observed seconds saveable (desc). Attach the improvement + its
    declared effort tier; items with no measurable time saving rank last but are
    kept (they are still evidence-backed friction)."""
    def key(b):
        s = b.get("seconds_saveable")
        return (-(s if isinstance(s, (int, float)) else -1.0), b["signal"])

    ranked = []
    for i, b in enumerate(sorted(bottlenecks, key=key)):
        fix = _FRICTION_FIX.get(b["root_cause"], {"improvement": "investigate",
                                                  "effort": "unknown"})
        ranked.append({
            "rank": i + 1,
            "improvement": fix["improvement"],
            "root_cause": b["root_cause"],
            "effort": fix["effort"],
            "frequency": b["frequency"],
            "seconds_saveable": b.get("seconds_saveable"),
            "evidence": b["evidence"],
            "signal": b["signal"],
        })
    return ranked


def compare_before_after(before: Mapping[str, Any],
                         after: Mapping[str, Any]) -> dict[str, Any]:
    """Phase 5 — did an implemented improvement actually help? Compares the two
    reports' medians; NO IMPACT when nothing measurably improved."""
    keys = ("time_to_first_useful_evidence_ms", "time_to_understanding_ms",
            "time_to_confidence_ms", "time_to_decision_ms", "total_ms")
    b = before.get("operator_segment_median_ms", {}) or {}
    a = after.get("operator_segment_median_ms", {}) or {}
    deltas = {}
    improved = False
    for k in keys:
        bv, av = b.get(k), a.get(k)
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            d = round(bv - av, 1)
            deltas[k] = d
            if d > 0:
                improved = True
    return {
        "deltas_ms": deltas,
        "verdict": "IMPROVED" if improved else "NO_IMPACT",
    }


__all__ = [
    "IMPROVEMENT_ENGINE_SCHEMA_VERSION", "analyze", "compare_before_after",
]
