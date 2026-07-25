"""OIP Service #5 — Daily Operations Brief.

The shift-start handoff. When an OCC engineer, SRE, or ops lead comes on
shift they get one concise, action-first summary of the last operational
period: which services need attention, which applications are at risk, what
is getting worse, what keeps recurring, what changed, what to work on first —
and whether every recommendation can be verified.

This is an ORCHESTRATION layer: it composes the four shipped operator-facing
OIP services and assembles their outputs into handoff sections. It does not
bypass them, and it introduces NO new intelligence, NO new scoring, NO new
health model, NO runtime touch. Every recommendation carries the incident
ids that support it.

Reuses (does not duplicate):
  * ``oip.operational_health``   — estate + per-service health
  * ``oip.incident_trends``      — increasing / recurring / changed / actions
  * ``oip.application_health``   — per-application risk
  * ``oip.service_reliability``  — per-service reliability + direction

Deterministic, produce-only. No clock, no randomness.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.oip.application_health import application_health
from sentinel_core.oip.incident_trends import incident_trends
from sentinel_core.oip.operational_health import operational_health
from sentinel_core.oip.service_reliability import service_reliability

DAILY_OPERATIONS_BRIEF_SCHEMA_VERSION = 1


def daily_operations_brief(
    results: Iterable[Mapping[str, Any]],
    incidents: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    period: str = "",
) -> dict[str, Any]:
    """Assemble the shift-handoff brief from the four shipped OIP services."""
    incidents = incidents or {}
    results = list(results)

    # Compose the four operator-facing services (no bypass, no new logic).
    health = operational_health(results, incidents, period=period)
    trends = incident_trends(results, incidents)
    apps = application_health(results, incidents)
    reliability = service_reliability(results, incidents)

    critical_services = _critical_services(reliability)
    applications_at_risk = _applications_at_risk(apps)

    corpus_stamped = sum(1 for r in results if r.get("_corpus_version"))

    brief = {
        "schema_version": DAILY_OPERATIONS_BRIEF_SCHEMA_VERSION,
        "period": period,
        "periods_covered": trends["periods"],
        "investigations": len(results),
        "headline": {
            "services_evaluated": health["services_evaluated"],
            "applications_evaluated": apps["applications_evaluated"],
            "critical_services": len(critical_services),
            "applications_at_risk": len(applications_at_risk),
            "increasing_trends": len(trends["what_is_increasing"]),
            "recurring_failures": len(trends["what_is_recurring"]),
            "priority_actions": len(trends["investigate_first"]),
        },
        "critical_services": critical_services,
        "applications_at_risk": applications_at_risk,
        "significant_incident_trends": trends["what_is_increasing"],
        "recurring_failures": trends["what_is_recurring"],
        "changed_since_previous": trends["changed_since_previous"],
        "highest_priority_actions": trends["investigate_first"],
        "verification_status": {
            "verifiable": trends["verifiable"],
            "investigations": len(results),
            "corpus_stamped": corpus_stamped,
        },
    }
    return brief


def _critical_services(reliability: dict[str, Any]) -> list[dict[str, Any]]:
    """Services needing attention (non-healthy), worst-first per the reused
    service_reliability attention order. Each carries its supporting incidents."""
    records = reliability["services"]
    out = []
    for svc in reliability["attention_order"]:
        s = records[svc]
        if s["reliability_band"] == "healthy":
            continue
        out.append({
            "service": s["service"],
            "owner": s["owner"],
            "reliability_band": s["reliability_band"],
            "reliability_score": s["reliability_score"],
            "reliability_direction": s["reliability_direction"],
            "why": s["why"],
            "fix_first": s["fix_first"],
            "evidence": s["affecting_incidents"],
            "verifiable": s["verifiable"],
        })
    return out


def _applications_at_risk(apps: dict[str, Any]) -> list[dict[str, Any]]:
    """Applications not fully healthy, worst-first per the reused
    application_health attention order. Each carries its driving incidents."""
    records = apps["applications"]
    out = []
    for app in apps["attention_order"]:
        a = records[app]
        if a["health_band"] == "healthy":
            continue
        out.append({
            "application": a["application"],
            "owner": a["owner"],
            "health_band": a["health_band"],
            "health_score": a["health_score"],
            "why": a["why"],
            "next_action": a["next_action"],
            "evidence": a["driving_incidents"],
            "verifiable": a["verifiable"],
        })
    return out


__all__ = ["DAILY_OPERATIONS_BRIEF_SCHEMA_VERSION", "daily_operations_brief"]
