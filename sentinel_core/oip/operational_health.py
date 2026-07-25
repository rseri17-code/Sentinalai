"""OIP Service #1 — Operational Health.

Answers, for every service in the estate: *is it healthy right now, why, with
what evidence, and what should I do next?* — the operator's first question at
2 AM. Pure composition over completed investigation outputs; NO new reasoning,
NO new intelligence, NO runtime touch.

Reuses (does not duplicate): ``shadow_pilot.observation_record`` (normalizes an
investigation result + incident into a compact per-incident record) and
``shadow_pilot.bucket_by`` (groups by service). Reads the R1 ``_corpus_version``
and R2 ``_evidence_lifecycle`` fields directly off the result for the
operator-facing evidence/verifiability view.

The operator never sees "Replay", "Frozen Corpus", "ODE", or "provenance" — only
plain answers to: what happened · why · what proves it · what changed · who owns
it · what to do next · how confident · can I verify it.

Deterministic, produce-only, append-only-friendly. No clock, no randomness.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.shadow_pilot import (
    bucket_by,
    observation_record,
)

OPERATIONAL_HEALTH_SCHEMA_VERSION = 1

# health bands (deterministic thresholds over the composite score)
_HEALTHY = 0.80
_WATCH = 0.60


def _round(x: float) -> float:
    return round(float(x), 4)


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _verified(status: str) -> bool:
    return status in ("proves", "supports")


def operational_health(
    results: Iterable[Mapping[str, Any]],
    incidents: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    period: str = "",
) -> dict[str, Any]:
    """Per-service operational health rolled up from completed investigations.

    ``results`` — completed investigation result dicts.
    ``incidents`` — optional {incident_id: incident_metadata} for service/type.
    """
    incidents = incidents or {}
    results = list(results)

    # Normalize each investigation into a compact record (REUSE), keeping a
    # handle back to the raw result for the R1/R2 operator-facing fields.
    obs_pairs: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    for r in results:
        inc = incidents.get(str(r.get("incident_id", "")), {})
        obs_pairs.append((observation_record(r, inc), r))
    observations = [o for o, _ in obs_pairs]
    raw_by_incident = {o["incident_id"]: r for o, r in obs_pairs}

    groups = bucket_by(observations, "service")
    services = []
    for svc in sorted(groups):
        members = groups[svc]
        services.append(_service_health(svc, members, raw_by_incident))

    # Estate roll-up
    scores = [s["health_score"] for s in services
              if isinstance(s["health_score"], (int, float))]
    band_counts: dict[str, int] = {"healthy": 0, "watch": 0, "at_risk": 0}
    for s in services:
        band_counts[s["health_band"]] = band_counts.get(s["health_band"], 0) + 1

    return {
        "schema_version": OPERATIONAL_HEALTH_SCHEMA_VERSION,
        "period": period,
        "services_evaluated": len(services),
        "investigations": len(results),
        "estate_health_score": _round(_mean(scores)) if scores else None,
        "band_counts": band_counts,
        "attention_order": [s["service"] for s in sorted(
            services, key=lambda s: (s["health_score"], s["service"]))],
        "services": {s["service"]: s for s in services},
    }


def _service_health(service: str, members: list[Mapping[str, Any]],
                    raw_by_incident: Mapping[str, Any]) -> dict[str, Any]:
    cores = [m.get("core", {}) for m in members]
    n = len(members)

    # --- composite health score (all components in [0,1]) ---
    resolution = _mean([1.0 if _verified(c.get("verification_status", ""))
                        else 0.0 for c in cores])
    completeness = _mean([float(c["investigation_completeness"]) for c in cores
                          if isinstance(c.get("investigation_completeness"),
                                        (int, float))]) or 0.0
    confidence = _mean([float(c["evidence_confidence"]) / 100.0 for c in cores
                        if isinstance(c.get("evidence_confidence"),
                                      (int, float))]) or 0.0
    availability = _mean([0.0 if c.get("degraded_investigation") else 1.0
                          for c in cores])
    # reproducibility from the raw R1 stamp
    repro = _mean([1.0 if raw_by_incident.get(m["incident_id"], {}).get(
        "_corpus_version") else 0.0 for m in members])

    score = _round(0.30 * resolution + 0.25 * completeness
                   + 0.20 * confidence + 0.15 * availability + 0.10 * repro)
    band = ("healthy" if score >= _HEALTHY
            else "watch" if score >= _WATCH else "at_risk")

    # --- latest incident for the operator narrative (deterministic: by id) ---
    latest = sorted(members, key=lambda m: m["incident_id"])[-1]
    lc = latest.get("core", {})
    raw_latest = raw_by_incident.get(latest["incident_id"], {})
    lifecycle = raw_latest.get("_evidence_lifecycle", {}).get("counts", {})
    degraded = [d.get("source") for d in
                (raw_latest.get("_sources_unavailable") or [])]

    # recurring root causes (composition, not new mining)
    causes: dict[str, int] = {}
    for c in cores:
        rc = str(c.get("root_cause", ""))
        if rc:
            causes[rc] = causes.get(rc, 0) + 1
    recurring = sorted(([c, k] for c, k in causes.items() if k >= 2),
                       key=lambda x: (-x[1], x[0]))

    return {
        "service": service,
        "health_score": score,
        "health_band": band,
        "incidents": n,
        # --- the 8 operator questions, answered from existing evidence ---
        "what_happened": f"{n} investigation(s); latest: "
                         f"{lc.get('incident_type', 'incident')}",
        "why": str(lc.get("root_cause", "")) or "(unresolved)",
        "evidence": {
            "completeness": _round(completeness),
            "used": lifecycle.get("used", 0),
            "unavailable": lifecycle.get("unavailable", 0)
            + lifecycle.get("error", 0),
        },
        "what_changed": bool("deploy" in str(lc.get("root_cause", "")).lower()
                             or "change" in str(lc.get("root_cause", "")).lower()
                             or "deploy" in str(lc.get("incident_type",
                                                       "")).lower()),
        "owner": service,
        "next_action": _next_action(band, degraded, recurring),
        "confidence": int(lc.get("evidence_confidence") or 0),
        "verifiable": bool(raw_latest.get("_corpus_version")),
        "recurring_root_causes": recurring,
        "degraded_sources": sorted(s for s in degraded if s),
    }


def _next_action(band: str, degraded: list[str],
                 recurring: list[list]) -> str:
    if degraded:
        return ("restore evidence sources: "
                + ", ".join(sorted(s for s in degraded if s)))
    if recurring:
        return (f"address recurring root cause "
                f"'{recurring[0][0]}' ({recurring[0][1]}x)")
    if band == "at_risk":
        return "investigate: low verification / completeness"
    if band == "watch":
        return "monitor: confidence or completeness below target"
    return "no action — healthy"


__all__ = ["OPERATIONAL_HEALTH_SCHEMA_VERSION", "operational_health"]
