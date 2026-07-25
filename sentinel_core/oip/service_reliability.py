"""OIP Service #4 — Service Reliability.

Answers, for every service in the estate: *is it reliable, why, is reliability
improving or degrading, which incidents affect it, what recurring failures
reduce it, what should be fixed first, how confident, and can I verify it?* —
the SRE / service-owner view. Pure composition over completed investigation
outputs and the shipped OIP services; NO new reasoning, NO new reliability
model, NO new scoring, NO runtime touch.

Reuses (does not duplicate):
  * ``oip.operational_health`` — per-service health snapshot + bands
  * ``oip.incident_trends``    — recurring failures, what to investigate first
  * ``effectiveness._trend``   — improving/degrading direction math
  * ``incident_trends._period_of`` — deterministic period bucket
  * ``application_health._owner_of`` — existing ownership metadata

No reliability algorithm lives here. The reliability score IS
operational_health's per-service score; the reliability *direction* is
``_trend`` applied to the per-period operational_health score series (higher is
better). Deterministic, produce-only. No clock, no randomness.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.effectiveness import _trend
from sentinel_core.oip.application_health import _owner_of
from sentinel_core.oip.incident_trends import _period_of, incident_trends
from sentinel_core.oip.operational_health import operational_health

SERVICE_RELIABILITY_SCHEMA_VERSION = 1


def _round(x: float) -> float:
    return round(float(x), 4)


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def service_reliability(
    results: Iterable[Mapping[str, Any]],
    incidents: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per-service reliability rolled up from completed investigations."""
    incidents = incidents or {}
    results = list(results)

    # Group by the same service key operational_health assigns (inc.service).
    groups: dict[str, dict[str, Any]] = {}
    for r in results:
        iid = str(r.get("incident_id", ""))
        inc = incidents.get(iid, {})
        svc = str(inc.get("service", "") or "") or "(none)"
        g = groups.setdefault(svc, {"results": [], "incidents": {}})
        g["results"].append(r)
        if iid:
            g["incidents"][iid] = inc

    services = []
    for svc in sorted(groups):
        services.append(_service(svc, groups[svc]["results"],
                                 groups[svc]["incidents"]))

    scores = [s["reliability_score"] for s in services
              if isinstance(s["reliability_score"], (int, float))]
    band_counts: dict[str, int] = {"healthy": 0, "watch": 0, "at_risk": 0}
    for s in services:
        band_counts[s["reliability_band"]] = \
            band_counts.get(s["reliability_band"], 0) + 1

    return {
        "schema_version": SERVICE_RELIABILITY_SCHEMA_VERSION,
        "services_evaluated": len(services),
        "investigations": len(results),
        "estate_reliability_score": _round(_mean(scores)) if scores else None,
        "band_counts": band_counts,
        "attention_order": [s["service"] for s in sorted(
            services,
            key=lambda s: (s["reliability_score"], s["service"]))],
        "services": {s["service"]: s for s in services},
    }


def _service(svc: str, results: list[Mapping[str, Any]],
             svc_incidents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    # REUSE the shipped OIP services on this service's own incidents.
    health = operational_health(results, svc_incidents)["services"].get(svc, {})
    trends = incident_trends(results, svc_incidents)

    owner = _owner_of(svc_incidents)
    if owner == "(unowned)":
        owner = svc                                  # a service owns itself

    score = health.get("health_score", 0.0)
    band = health.get("health_band", "at_risk")
    trend = _reliability_trend(svc, results, svc_incidents)

    affecting = sorted(svc_incidents) if svc_incidents \
        else sorted(str(r.get("incident_id", "")) for r in results
                    if r.get("incident_id"))

    return {
        "service": svc,
        "owner": owner,
        "reliable": band == "healthy",
        "reliability_score": score,
        "reliability_band": band,
        "reliability_direction": trend["direction"],
        "incidents": len(results),
        # --- operator questions, answered from existing evidence ---
        "why": health.get("why", "(unresolved)"),
        "affecting_incidents": affecting,
        "recurring_failures": trends["what_is_recurring"],
        "evidence": health.get("evidence", {"used": 0, "unavailable": 0,
                                             "completeness": 0.0}),
        "confidence": health.get("confidence", 0),
        "verifiable": bool(health.get("verifiable")),
        "fix_first": _fix_first(trends, health),
        # --- attribution back to the composed services ---
        "reliability_trend": trend,
        "operational_health": {
            "health_score": health.get("health_score"),
            "health_band": health.get("health_band"),
            "degraded_sources": health.get("degraded_sources", []),
            "next_action": health.get("next_action", ""),
        },
        "incident_trends": {
            "periods": trends["periods"],
            "changed_since_previous": trends["changed_since_previous"],
            "investigate_first": trends["investigate_first"],
        },
    }


def _reliability_trend(svc: str, results: list[Mapping[str, Any]],
                       incidents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Improving/degrading direction from the per-period operational_health
    score series (REUSE operational_health + _trend). Higher score is better,
    so desired='up'."""
    by_period: dict[str, dict[str, Any]] = {}
    for r in results:
        iid = str(r.get("incident_id", ""))
        p = _period_of(incidents.get(iid, {}))
        if p and p != "(undated)":
            b = by_period.setdefault(p, {"results": [], "incidents": {}})
            b["results"].append(r)
            if iid:
                b["incidents"][iid] = incidents.get(iid, {})

    periods = sorted(by_period)
    series: list[float] = []
    for p in periods:
        rec = operational_health(by_period[p]["results"],
                                 by_period[p]["incidents"])["services"].get(svc)
        if rec is not None:
            series.append(float(rec["health_score"]))

    if len(series) >= 2:
        tr = _trend(series, "up")
        direction = ("improving" if tr["verdict"] == "improving"
                     else "degrading" if tr["verdict"] == "degrading"
                     else "stable")
        slope = tr.get("slope", 0.0)
    else:
        direction, slope = "insufficient_history", 0.0

    return {"direction": direction, "slope": slope, "periods": periods,
            "series": [_round(x) for x in series]}


def _fix_first(trends: dict[str, Any], health: dict[str, Any]) -> str:
    first = trends["investigate_first"]
    if first:
        top = first[0]
        return (f"fix {top['priority'].replace('_', ' ')}: {top['target']}")
    return health.get("next_action", "no action — reliable")


__all__ = ["SERVICE_RELIABILITY_SCHEMA_VERSION", "service_reliability"]
