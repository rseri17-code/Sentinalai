"""OIP Service #2 — Incident Trends.

Answers, across the estate over time: *what is increasing, what keeps
recurring, what changed since last period, which services are getting worse,
and what should I investigate first?* Pure composition over completed
investigation outputs; NO new reasoning, NO new scoring, NO runtime touch.

Reuses (does not duplicate):
  * ``shadow_pilot.observation_record`` — normalize result + incident
  * ``shadow_pilot.bucket_by``           — group by incident class
  * ``effectiveness._trend``             — slope/direction/verdict trend math
  * ``oip.operational_health``           — per-period service-health decline

The operator sees plain answers traceable to investigation artifacts, never
the internals. Deterministic, produce-only, no clock, no randomness.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.effectiveness import _trend
from sentinel_core.investigation_value.shadow_pilot import (
    bucket_by,
    observation_record,
)
from sentinel_core.oip.operational_health import operational_health

INCIDENT_TRENDS_SCHEMA_VERSION = 1

_RECUR_MIN = 2            # a root cause is "recurring" at >= this many incidents


def _round(x: float) -> float:
    return round(float(x), 4)


def _period_of(incident: Mapping[str, Any]) -> str:
    """Deterministic period bucket from the incident's own timestamp (no
    wall-clock). Prefers an explicit ``period``; else the created_at date."""
    p = str(incident.get("period", "") or "")
    if p:
        return p
    ts = str(incident.get("created_at", "") or incident.get("start_time", ""))
    return ts[:10] if len(ts) >= 10 else "(undated)"


def incident_trends(
    results: Iterable[Mapping[str, Any]],
    incidents: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Estate-wide incident trends composed from completed investigations."""
    incidents = incidents or {}
    results = list(results)

    obs_pairs = []
    for r in results:
        inc = incidents.get(str(r.get("incident_id", "")), {})
        obs_pairs.append((observation_record(
            r, inc, observed_period=_period_of(inc)), r, inc))
    observations = [o for o, _, _ in obs_pairs]
    periods = sorted({o["observed_period"] for o in observations
                      if o["observed_period"] and o["observed_period"]
                      != "(undated)"})

    class_trends = _class_trends(observations, periods)
    recurring = _recurring_failures(observations)
    period_delta = _period_delta(obs_pairs, periods)
    health_decline = _health_decline(obs_pairs, periods)

    # What to investigate first: increasing volume × recurrence × health decline.
    investigate = _investigate_first(class_trends, recurring, health_decline)

    verifiable = all(bool(r.get("_corpus_version")) for _, r, _ in obs_pairs) \
        if obs_pairs else True

    return {
        "schema_version": INCIDENT_TRENDS_SCHEMA_VERSION,
        "investigations": len(results),
        "periods": periods,
        "what_is_increasing": [t for t in class_trends
                               if t["verdict"] == "increasing"],
        "class_trends": class_trends,
        "what_is_recurring": recurring,
        "changed_since_previous": period_delta,
        "services_getting_worse": health_decline,
        "investigate_first": investigate,
        "verifiable": verifiable,
    }


def _class_trends(observations, periods) -> list[dict[str, Any]]:
    """Per incident-class volume trend over ordered periods (REUSE _trend).
    Fewer incidents is better, so desired='down'; verdict 'degrading' => the
    class is INCREASING (operator-adverse)."""
    by_class = bucket_by(observations, "incident_class")
    out = []
    for cls in sorted(by_class):
        members = by_class[cls]
        counts_by_period = {p: 0 for p in periods}
        for m in members:
            p = m["observed_period"]
            if p in counts_by_period:
                counts_by_period[p] += 1
        series = [float(counts_by_period[p]) for p in periods]
        tr = _trend(series, "down") if len(series) >= 2 else {
            "verdict": "flat", "slope": 0.0, "direction": "flat",
            "first": series[0] if series else None,
            "last": series[-1] if series else None, "periods": len(series)}
        # rename to operator language: degrading (down-desired) == increasing
        verdict = ("increasing" if tr["verdict"] == "degrading"
                   else "decreasing" if tr["verdict"] == "improving"
                   else "flat")
        out.append({
            "incident_class": cls,
            "verdict": verdict,
            "slope": tr.get("slope", 0.0),
            "total": len(members),
            "series": [int(c) for c in series],
            "evidence": sorted(m["incident_id"] for m in members),
        })
    # increasing first, by steepest slope
    return sorted(out, key=lambda t: (t["verdict"] != "increasing",
                                      -t["slope"], t["incident_class"]))


def _recurring_failures(observations) -> list[dict[str, Any]]:
    """Root causes recurring across the corpus (composition — no new mining)."""
    by_cause: dict[str, list[str]] = {}
    cause_class: dict[str, str] = {}
    for o in observations:
        rc = str(o.get("core", {}).get("root_cause", ""))
        if not rc:
            continue
        by_cause.setdefault(rc, []).append(o["incident_id"])
        cause_class.setdefault(rc, o.get("core", {}).get("incident_type", ""))
    out = []
    for rc in sorted(by_cause):
        ids = by_cause[rc]
        if len(ids) >= _RECUR_MIN:
            out.append({"root_cause": rc, "incident_class": cause_class[rc],
                        "count": len(ids),
                        "evidence": sorted(ids)})
    return sorted(out, key=lambda r: (-r["count"], r["root_cause"]))


def _period_delta(obs_pairs, periods) -> dict[str, Any]:
    """Per-class count delta between the two most recent periods."""
    if len(periods) < 2:
        return {"available": False, "reason": "need >= 2 periods"}
    prev, curr = periods[-2], periods[-1]
    def counts(period):
        c: dict[str, int] = {}
        for o, _, _ in obs_pairs:
            if o["observed_period"] == period:
                k = o.get("core", {}).get("incident_type", "") or "(none)"
                c[k] = c.get(k, 0) + 1
        return c
    pc, cc = counts(prev), counts(curr)
    classes = sorted(set(pc) | set(cc))
    return {
        "available": True, "previous": prev, "current": curr,
        "by_class": {k: {"previous": pc.get(k, 0), "current": cc.get(k, 0),
                         "delta": cc.get(k, 0) - pc.get(k, 0)}
                     for k in classes},
    }


def _health_decline(obs_pairs, periods) -> list[dict[str, Any]]:
    """Services whose Operational Health score fell between the two most recent
    periods (REUSE operational_health)."""
    if len(periods) < 2:
        return []
    prev, curr = periods[-2], periods[-1]

    def health_for(period):
        results = [r for o, r, _ in obs_pairs if o["observed_period"] == period]
        incs = {str(o["incident_id"]): i for o, _, i in obs_pairs
                if o["observed_period"] == period}
        return operational_health(results, incs)["services"]

    hp, hc = health_for(prev), health_for(curr)
    out = []
    for svc in sorted(set(hp) & set(hc)):
        drop = _round(hp[svc]["health_score"] - hc[svc]["health_score"])
        if drop > 0.0:
            out.append({"service": svc, "previous_score": hp[svc]["health_score"],
                        "current_score": hc[svc]["health_score"],
                        "decline": drop, "why": hc[svc]["why"]})
    return sorted(out, key=lambda s: (-s["decline"], s["service"]))


def _investigate_first(class_trends, recurring, health_decline) -> list[dict[str, Any]]:
    """Rank what deserves attention first: increasing classes, recurring
    failures, and declining services — highest-signal first."""
    items = []
    for t in class_trends:
        if t["verdict"] == "increasing":
            items.append({"priority": "increasing_incident_class",
                          "target": t["incident_class"], "signal": t["slope"],
                          "evidence": t["evidence"]})
    for r in recurring:
        items.append({"priority": "recurring_failure", "target": r["root_cause"],
                      "signal": float(r["count"]), "evidence": r["evidence"]})
    for s in health_decline:
        items.append({"priority": "service_health_decline",
                      "target": s["service"], "signal": s["decline"],
                      "evidence": []})
    return sorted(items, key=lambda x: (-x["signal"], x["priority"],
                                        str(x["target"])))[:10]


__all__ = ["INCIDENT_TRENDS_SCHEMA_VERSION", "incident_trends"]
