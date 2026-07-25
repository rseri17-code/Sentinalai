"""OIP Service #3 — Application Health.

Answers, for every application in the estate: *is it healthy, why, with what
evidence, what changed, which incidents drive it, what recurs, who owns it,
what to do first, how confident, and can I verify it?* — the application
owner's dashboard. Pure composition over completed investigation outputs and
the two shipped OIP services; NO new reasoning, NO new scoring, NO runtime
touch.

An application groups one or more services. This service therefore:
  * groups results by the incident's ``application`` (existing dimension),
  * rolls each application up with ``oip.operational_health`` (per-service
    health + estate score + bands),
  * summarises each application's movement with ``oip.incident_trends``
    (what is increasing / recurring / changed / worth investigating first).

No health algorithm lives here. The application score IS operational_health's
rollup for the application's own incidents; the application band is composed
from its services' existing bands (worst-of) — no new thresholds. Ownership,
confidence, evidence and verifiability are read straight off existing fields.

Deterministic, produce-only. No clock, no randomness.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.oip.incident_trends import incident_trends
from sentinel_core.oip.operational_health import operational_health

APPLICATION_HEALTH_SCHEMA_VERSION = 1

# Order of increasing concern — used only to pick the worst existing band.
_BAND_RANK = {"healthy": 0, "watch": 1, "at_risk": 2}


def _round(x: float) -> float:
    return round(float(x), 4)


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _application_of(incident: Mapping[str, Any],
                    result: Mapping[str, Any]) -> str:
    """Existing application dimension; a lone service is its own application."""
    return (str(incident.get("application", "") or "")
            or str(incident.get("service", "") or "")
            or "(unmapped)")


def _owner_of(app_incidents: Mapping[str, Mapping[str, Any]]) -> str:
    """Most-common existing ownership metadata (``owner`` or ``team``)."""
    counts: dict[str, int] = {}
    for inc in app_incidents.values():
        owner = str(inc.get("owner", "") or inc.get("team", "") or "")
        if owner:
            counts[owner] = counts.get(owner, 0) + 1
    if not counts:
        return "(unowned)"
    return sorted(counts, key=lambda o: (-counts[o], o))[0]


def application_health(
    results: Iterable[Mapping[str, Any]],
    incidents: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per-application health rolled up from completed investigations."""
    incidents = incidents or {}
    results = list(results)

    # Group results + incidents by application (existing dimension).
    groups: dict[str, dict[str, Any]] = {}
    for r in results:
        iid = str(r.get("incident_id", ""))
        inc = incidents.get(iid, {})
        app = _application_of(inc, r)
        g = groups.setdefault(app, {"results": [], "incidents": {}})
        g["results"].append(r)
        if iid:
            g["incidents"][iid] = inc

    applications = []
    for app in sorted(groups):
        applications.append(_application(app, groups[app]["results"],
                                         groups[app]["incidents"]))

    scores = [a["health_score"] for a in applications
              if isinstance(a["health_score"], (int, float))]
    band_counts: dict[str, int] = {"healthy": 0, "watch": 0, "at_risk": 0}
    for a in applications:
        band_counts[a["health_band"]] = band_counts.get(a["health_band"], 0) + 1

    return {
        "schema_version": APPLICATION_HEALTH_SCHEMA_VERSION,
        "applications_evaluated": len(applications),
        "investigations": len(results),
        "estate_health_score": _round(_mean(scores)) if scores else None,
        "band_counts": band_counts,
        "attention_order": [a["application"] for a in sorted(
            applications, key=lambda a: (a["health_score"], a["application"]))],
        "applications": {a["application"]: a for a in applications},
    }


def _application(app: str, results: list[Mapping[str, Any]],
                 app_incidents: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    # REUSE the two shipped OIP services on this application's own incidents.
    health = operational_health(results, app_incidents)
    trends = incident_trends(results, app_incidents)

    svc_records = health["services"]
    services = sorted(svc_records)

    # Application score = operational_health's rollup for this application.
    score = health["estate_health_score"] if health["estate_health_score"] \
        is not None else 0.0
    # Application band = worst existing service band (no new thresholds).
    band = "healthy"
    for s in svc_records.values():
        if _BAND_RANK.get(s["health_band"], 0) > _BAND_RANK.get(band, 0):
            band = s["health_band"]

    # Evidence / confidence / verifiability — read straight off the services.
    used = sum(s["evidence"]["used"] for s in svc_records.values())
    unavailable = sum(s["evidence"]["unavailable"] for s in svc_records.values())
    completeness = _round(_mean([s["evidence"]["completeness"]
                                 for s in svc_records.values()]))
    confidence = int(round(_mean([float(s["confidence"])
                                  for s in svc_records.values()])))
    verifiable = bool(svc_records) and all(
        s["verifiable"] for s in svc_records.values())

    # Incidents driving the score: those in non-healthy services (worst first).
    band_of_service = {name: s["health_band"] for name, s in svc_records.items()}
    driving = sorted(
        (iid for iid, inc in app_incidents.items()
         if band_of_service.get(str(inc.get("service", "")), "healthy")
         != "healthy"),
        key=lambda iid: (
            -_BAND_RANK.get(band_of_service.get(
                str(app_incidents[iid].get("service", "")), "healthy"), 0),
            iid))

    # Recurring failures across the application (from incident_trends).
    recurring = trends["what_is_recurring"]

    what_changed = any(s["what_changed"] for s in svc_records.values())

    return {
        "application": app,
        "owner": _owner_of(app_incidents),
        "health_score": score,
        "health_band": band,
        "is_healthy": band == "healthy",
        "services": services,
        "services_evaluated": len(services),
        "incidents": len(results),
        # --- operator questions, answered from existing evidence ---
        "why": _why(band, health),
        "evidence": {"used": used, "unavailable": unavailable,
                     "completeness": completeness},
        "what_changed": what_changed,
        "driving_incidents": driving,
        "recurring_root_causes": recurring,
        "what_is_increasing": trends["what_is_increasing"],
        "next_action": _next_action(band, trends, health),
        "confidence": confidence,
        "verifiable": verifiable,
        # --- full attribution back to the composed services ---
        "operational_health": {
            "estate_health_score": health["estate_health_score"],
            "band_counts": health["band_counts"],
            "attention_order": health["attention_order"],
        },
        "incident_trends": {
            "periods": trends["periods"],
            "changed_since_previous": trends["changed_since_previous"],
            "investigate_first": trends["investigate_first"],
        },
    }


def _why(band: str, health: dict[str, Any]) -> str:
    if band == "healthy":
        return "all services healthy"
    worst = health["attention_order"][0] if health["attention_order"] else ""
    svc = health["services"].get(worst, {})
    return (f"{band}: worst service '{worst}' — "
            f"{svc.get('why', '(unresolved)')}")


def _next_action(band: str, trends: dict[str, Any],
                 health: dict[str, Any]) -> str:
    first = trends["investigate_first"]
    if first:
        top = first[0]
        return (f"investigate {top['priority'].replace('_', ' ')}: "
                f"{top['target']}")
    worst = health["attention_order"][0] if health["attention_order"] else ""
    svc = health["services"].get(worst, {})
    return svc.get("next_action", "no action — healthy")


__all__ = ["APPLICATION_HEALTH_SCHEMA_VERSION", "application_health"]
