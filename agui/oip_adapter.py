"""Adapter: runtime investigation state -> Operational Intelligence inputs.

This is the convergence seam. It maps completed ``IncidentState`` records (the
runtime source of truth, persisted by the investigation pipeline) into the
plain result/incident dicts that ``sentinel_core.oip.operational_health``
consumes. The OIP layer stays the single source of aggregation logic — this
adapter only reshapes existing data. It NEVER invents signals: fields the
default runtime does not produce (validation/causal shadow signals, off by
default) are simply left absent, and Operational Health reflects that honestly.
"""
from __future__ import annotations

from typing import Any, Iterable

from sentinel_core.models.incidents import IncidentState, InvestigationStatus


def _incident_id(state: IncidentState) -> str:
    return str(state.incident_id or state.investigation_id or "")


def states_to_oip_inputs(
    states: Iterable[IncidentState],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Return (results, incidents, drilldown) for completed investigations.

    * ``results``   — result dicts for ``operational_health`` (only fields the
      runtime actually produces; R1 corpus stamp + R2 evidence lifecycle when
      present; localization service from the incident's affected service).
    * ``incidents`` — {incident_id: metadata} (service, type, severity).
    * ``drilldown`` — {service: investigation_id} so the operator can open the
      supporting investigation for the service shown as worst.
    """
    results: list[dict[str, Any]] = []
    incidents: dict[str, dict[str, Any]] = {}
    drilldown: dict[str, str] = {}

    completed = [s for s in states
                 if s.status == InvestigationStatus.COMPLETED]
    # Deterministic order: newest first, so drilldown resolves to the latest
    # investigation per service.
    completed.sort(key=lambda s: (s.completed_at or s.started_at or ""),
                   reverse=True)

    for s in completed:
        iid = _incident_id(s)
        if not iid:
            continue
        service = str(s.affected_service or "")
        itype = str(s.incident_type or "")

        result: dict[str, Any] = {
            "incident_id": iid,
            "root_cause": str(s.root_cause or ""),
            "confidence": int(round(float(s.confidence or 0.0))),
            "incident_type": itype,
            # localization from the incident's own service (runtime-available)
            "_causal_investigation": {
                "localization": {"root_cause_service": service}},
        }
        if s.corpus_version:                       # R1 reproducibility stamp
            result["_corpus_version"] = str(s.corpus_version)
        if isinstance(s.evidence_lifecycle, dict) and s.evidence_lifecycle:
            result["_evidence_lifecycle"] = s.evidence_lifecycle

        results.append(result)
        incidents[iid] = {
            "incident_id": iid,
            "service": service,
            "incident_type": itype,
            "severity": s.severity,
        }
        # first (newest) wins per service
        if service and service not in drilldown:
            drilldown[service] = str(s.investigation_id or iid)

    return results, incidents, drilldown


# Signals the composite health score depends on but which the DEFAULT runtime
# does not emit (they come from shadow engines that are off by default). The
# endpoint surfaces this so a low score reads as "signal unavailable", not
# "service failing".
DEFERRED_SIGNALS = (
    "root_cause_verification (validation engine)",
    "investigation_completeness (validation engine)",
    "evidence_confidence (validation engine)",
)


def signal_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Honest disclosure of which health-score inputs are actually present."""
    n = len(results)
    with_corpus = sum(1 for r in results if r.get("_corpus_version"))
    with_lifecycle = sum(1 for r in results if r.get("_evidence_lifecycle"))
    with_validation = sum(1 for r in results
                          if r.get("_investigation_validation"))
    return {
        "investigations": n,
        "with_corpus_version": with_corpus,
        "with_evidence_lifecycle": with_lifecycle,
        "with_validation_signals": with_validation,
        "deferred_signals": list(DEFERRED_SIGNALS),
        "note": ("Health composite is limited to reproducibility + evidence "
                 "availability when validation signals are absent; scores are "
                 "not fabricated to fill the gap."),
    }


__all__ = ["states_to_oip_inputs", "signal_coverage", "DEFERRED_SIGNALS"]
