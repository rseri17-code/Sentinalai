"""IaC Drift Detector — identifies when live infrastructure has drifted from its
IaC (Terraform / Helm / K8s manifest) baseline and flags that drift as a
first-class root cause of the current incident.

Key insight: "The config was manually changed 3 weeks ago and never committed
back to IaC.  That manual change is what caused tonight's incident."

Human-in-the-loop gate (mirrors fix_engine.py pattern):
  detected → reviewing → approved → rolled_back | dismissed

Usage::

    from supervisor.iac_drift_detector import detect_drift, approve_rollback

    report = detect_drift(
        service="payment-service",
        live_config=live_snapshot,
        iac_baseline=baseline,
        incident_context={"started_at": "2026-04-19T02:00:00Z"},
        iac_source="helm",
    )
    if report.drift_is_likely_root_cause:
        report = approve_rollback(report, approved_by="sre-alice")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("sentinalai.iac_drift_detector")

# ---------------------------------------------------------------------------
# Enums & Data-classes
# ---------------------------------------------------------------------------


class DriftSeverity(str, Enum):
    LOW = "low"           # cosmetic, no operational impact
    MEDIUM = "medium"     # may cause operational issues
    HIGH = "high"         # likely caused the current incident
    CRITICAL = "critical" # known to cause outages


@dataclass
class DriftedProperty:
    resource_type: str   # "deployment", "configmap", "service", "hpa", "pdb"
    resource_name: str   # e.g. "payment-service"
    property_path: str   # e.g. "spec.containers[0].resources.limits.memory"
    iac_value: Any       # what IaC says it should be
    live_value: Any      # what's actually running
    severity: DriftSeverity

    # Attribution (best-effort, may be None if CMDB audit log unavailable)
    changed_by: str | None  # who changed it
    changed_at: str | None  # when (ISO 8601)

    # Is this drift likely the incident root cause?
    incident_correlation: float  # 0.0–1.0
    correlation_reasoning: str


@dataclass
class DriftReport:
    service: str
    iac_source: str      # "terraform" | "helm" | "k8s_manifest" | "unknown"
    snapshot_time: str   # ISO 8601 timestamp of the live snapshot
    drifted_properties: list[DriftedProperty]
    total_drift_count: int
    high_severity_count: int  # HIGH + CRITICAL combined

    # Is drift likely the incident cause?
    drift_is_likely_root_cause: bool
    root_cause_confidence: float  # 0.0–1.0

    # Remediation (requires human approval)
    remediation_command: str     # e.g. "helm upgrade payment-service …"
    remediation_plan: list[str]  # step-by-step rollback to IaC
    requires_human_approval: bool  # always True

    # Status tracking (human-in-the-loop)
    status: str          # "detected"|"reviewing"|"approved"|"rolled_back"|"dismissed"
    approved_by: str | None


# ---------------------------------------------------------------------------
# Severity heuristics
# ---------------------------------------------------------------------------

# Ordered list of (substring_in_path, severity).  First match wins.
_SEVERITY_RULES: list[tuple[str, DriftSeverity]] = [
    # CRITICAL — wrong version running
    ("image", DriftSeverity.CRITICAL),
    # HIGH — resource / scaling / probe / env
    ("resources.limits", DriftSeverity.HIGH),
    ("replicas", DriftSeverity.HIGH),
    ("env", DriftSeverity.HIGH),
    ("configmap", DriftSeverity.HIGH),
    ("livenessProbe", DriftSeverity.HIGH),
    ("readinessProbe", DriftSeverity.HIGH),
    # MEDIUM — requests & HPA bounds
    ("resources.requests", DriftSeverity.MEDIUM),
    ("hpa.maxReplicas", DriftSeverity.MEDIUM),
    ("hpa.minReplicas", DriftSeverity.MEDIUM),
    # LOW — cosmetic
    ("annotations", DriftSeverity.LOW),
    ("labels", DriftSeverity.LOW),
]

_BASE_CORRELATION: dict[DriftSeverity, float] = {
    DriftSeverity.CRITICAL: 0.9,
    DriftSeverity.HIGH: 0.8,
    DriftSeverity.MEDIUM: 0.5,
    DriftSeverity.LOW: 0.2,
}


def _severity_for_path(property_path: str) -> DriftSeverity:
    """Return the drift severity for a given dot-notation property path."""
    for fragment, severity in _SEVERITY_RULES:
        if fragment in property_path:
            return severity
    # Default: MEDIUM — something changed, not sure of the impact
    return DriftSeverity.MEDIUM


def _compute_correlation(
    severity: DriftSeverity,
    changed_at: str | None,
    incident_context: dict[str, Any] | None,
) -> tuple[float, str]:
    """Return (correlation_score, reasoning_string).

    Boosts base score if the property was changed within 24 h of the
    incident start time.
    """
    base = _BASE_CORRELATION[severity]
    reasoning_parts = [
        f"{severity.value.upper()} severity drift → base correlation {base:.1f}"
    ]

    boost = 0.0
    if changed_at and incident_context:
        incident_start = incident_context.get("started_at") or incident_context.get(
            "incident_time"
        )
        if incident_start:
            try:
                dt_changed = datetime.fromisoformat(
                    changed_at.replace("Z", "+00:00")
                )
                dt_incident = datetime.fromisoformat(
                    incident_start.replace("Z", "+00:00")
                )
                # Ensure both are offset-aware
                if dt_changed.tzinfo is None:
                    dt_changed = dt_changed.replace(tzinfo=timezone.utc)
                if dt_incident.tzinfo is None:
                    dt_incident = dt_incident.replace(tzinfo=timezone.utc)

                delta_hours = abs((dt_incident - dt_changed).total_seconds()) / 3600
                if delta_hours <= 24:
                    boost = 0.15
                    reasoning_parts.append(
                        f"change occurred {delta_hours:.1f}h before incident → +0.15 boost"
                    )
            except (ValueError, TypeError):
                pass  # malformed timestamp — skip boost

    score = min(1.0, base + boost)
    return score, "; ".join(reasoning_parts)


# ---------------------------------------------------------------------------
# Recursive dict comparison
# ---------------------------------------------------------------------------


def _compare_nested(
    path: str,
    live: Any,
    baseline: Any,
    drifts: list[DriftedProperty],
    resource_type: str,
    resource_name: str,
    incident_context: dict[str, Any] | None = None,
    attribution: dict[str, Any] | None = None,
) -> None:
    """Recursively compare *live* against *baseline*.

    Keys present in baseline but missing/different in live are recorded as
    DriftedProperty entries appended to *drifts*.  Extra keys in live (not
    in baseline) are ignored — IaC drift only cares about deviation from the
    declared state, not undeclared additions.
    """
    if isinstance(baseline, dict) and isinstance(live, dict):
        for key, b_val in baseline.items():
            child_path = f"{path}.{key}" if path else key
            l_val = live.get(key, _MISSING)
            if l_val is _MISSING:
                # Key is in IaC but missing from live config — treat as drift
                _record_drift(
                    drifts=drifts,
                    path=child_path,
                    iac_value=b_val,
                    live_value=None,
                    resource_type=resource_type,
                    resource_name=resource_name,
                    incident_context=incident_context,
                    attribution=attribution,
                )
            else:
                _compare_nested(
                    path=child_path,
                    live=l_val,
                    baseline=b_val,
                    drifts=drifts,
                    resource_type=resource_type,
                    resource_name=resource_name,
                    incident_context=incident_context,
                    attribution=attribution,
                )
    elif isinstance(baseline, list) and isinstance(live, list):
        # Compare lists element-by-element up to the length of the baseline
        for idx, b_item in enumerate(baseline):
            child_path = f"{path}[{idx}]"
            if idx < len(live):
                _compare_nested(
                    path=child_path,
                    live=live[idx],
                    baseline=b_item,
                    drifts=drifts,
                    resource_type=resource_type,
                    resource_name=resource_name,
                    incident_context=incident_context,
                    attribution=attribution,
                )
            else:
                _record_drift(
                    drifts=drifts,
                    path=child_path,
                    iac_value=b_item,
                    live_value=None,
                    resource_type=resource_type,
                    resource_name=resource_name,
                    incident_context=incident_context,
                    attribution=attribution,
                )
    else:
        # Leaf comparison
        if live != baseline:
            _record_drift(
                drifts=drifts,
                path=path,
                iac_value=baseline,
                live_value=live,
                resource_type=resource_type,
                resource_name=resource_name,
                incident_context=incident_context,
                attribution=attribution,
            )


# Sentinel object to distinguish "key missing" from "key present with None value"
_MISSING = object()


def _record_drift(
    drifts: list[DriftedProperty],
    path: str,
    iac_value: Any,
    live_value: Any,
    resource_type: str,
    resource_name: str,
    incident_context: dict[str, Any] | None,
    attribution: dict[str, Any] | None,
) -> None:
    """Build a DriftedProperty and append to *drifts*."""
    severity = _severity_for_path(path)

    changed_by: str | None = None
    changed_at: str | None = None
    if attribution:
        # Attribution may be keyed by property_path or be a flat dict
        prop_attr = attribution.get(path) or {}
        if isinstance(prop_attr, dict):
            changed_by = prop_attr.get("changed_by") or attribution.get("changed_by")
            changed_at = prop_attr.get("changed_at") or attribution.get("changed_at")
        else:
            changed_by = attribution.get("changed_by")
            changed_at = attribution.get("changed_at")

    correlation, reasoning = _compute_correlation(severity, changed_at, incident_context)

    drifts.append(
        DriftedProperty(
            resource_type=resource_type,
            resource_name=resource_name,
            property_path=path,
            iac_value=iac_value,
            live_value=live_value,
            severity=severity,
            changed_by=changed_by,
            changed_at=changed_at,
            incident_correlation=correlation,
            correlation_reasoning=reasoning,
        )
    )


# ---------------------------------------------------------------------------
# Remediation command generation
# ---------------------------------------------------------------------------

_REMEDIATION_COMMANDS: dict[str, str] = {
    "helm": (
        "helm upgrade {service} ./charts/{service} --reuse-values"
    ),
    "terraform": (
        "terraform apply -target=module.{service} -auto-approve"
    ),
    "k8s_manifest": (
        "kubectl apply -f k8s/{service}/ --namespace default"
    ),
    "unknown": (
        "# Review IaC source and apply the appropriate reconciliation command for {service}"
    ),
}


def _build_remediation(
    service: str, iac_source: str, drifted_properties: list[DriftedProperty]
) -> tuple[str, list[str]]:
    """Return (remediation_command, remediation_plan)."""
    tmpl = _REMEDIATION_COMMANDS.get(iac_source, _REMEDIATION_COMMANDS["unknown"])
    cmd = tmpl.format(service=service)

    high_count = sum(
        1
        for dp in drifted_properties
        if dp.severity in (DriftSeverity.HIGH, DriftSeverity.CRITICAL)
    )

    plan: list[str] = [
        f"1. Review all {len(drifted_properties)} drifted properties listed in the report.",
        f"2. Confirm {high_count} high/critical drift(s) are safe to revert automatically.",
        "3. Create a change ticket and notify the on-call team.",
        f"4. Execute: {cmd}",
        "5. Monitor service health metrics for 10 minutes post-rollback.",
        "6. Commit any approved manual overrides back to the IaC repository.",
    ]
    return cmd, plan


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_drift(
    service: str,
    live_config: dict[str, Any],
    iac_baseline: dict[str, Any],
    incident_context: dict[str, Any] | None = None,
    iac_source: str = "terraform",
) -> DriftReport:
    """Compare *live_config* against *iac_baseline* and identify drift.

    Args:
        service: Name of the service being evaluated.
        live_config: Current running configuration snapshot (from CMDB / K8s API).
        iac_baseline: Desired-state configuration declared in IaC.
        incident_context: Optional incident details used for correlation.
            Recognised keys: ``started_at`` (ISO 8601), ``incident_time``.
            Also accepts ``resource_type``, ``resource_name``, and
            ``attribution`` for richer drift records.
        iac_source: One of ``"terraform"``, ``"helm"``, ``"k8s_manifest"``,
            ``"unknown"``.

    Returns:
        A :class:`DriftReport` with status ``"detected"`` and
        ``requires_human_approval=True``.
    """
    resource_type: str = (
        incident_context.get("resource_type", "deployment") if incident_context else "deployment"
    )
    resource_name: str = (
        incident_context.get("resource_name", service) if incident_context else service
    )
    attribution: dict[str, Any] | None = (
        incident_context.get("attribution") if incident_context else None
    )

    drifts: list[DriftedProperty] = []
    _compare_nested(
        path="",
        live=live_config,
        baseline=iac_baseline,
        drifts=drifts,
        resource_type=resource_type,
        resource_name=resource_name,
        incident_context=incident_context,
        attribution=attribution,
    )

    # Strip leading dot from paths produced by the recursive helper
    for dp in drifts:
        if dp.property_path.startswith("."):
            dp.property_path = dp.property_path[1:]

    high_count = sum(
        1
        for dp in drifts
        if dp.severity in (DriftSeverity.HIGH, DriftSeverity.CRITICAL)
    )

    max_correlation = max((dp.incident_correlation for dp in drifts), default=0.0)
    drift_is_root_cause = any(dp.incident_correlation > 0.7 for dp in drifts)
    root_cause_confidence = max_correlation

    remediation_command, remediation_plan = _build_remediation(service, iac_source, drifts)

    snapshot_time = datetime.now(timezone.utc).isoformat()

    report = DriftReport(
        service=service,
        iac_source=iac_source,
        snapshot_time=snapshot_time,
        drifted_properties=drifts,
        total_drift_count=len(drifts),
        high_severity_count=high_count,
        drift_is_likely_root_cause=drift_is_root_cause,
        root_cause_confidence=root_cause_confidence,
        remediation_command=remediation_command,
        remediation_plan=remediation_plan,
        requires_human_approval=True,
        status="detected",
        approved_by=None,
    )

    logger.info(
        "Drift detection complete: service=%s drifts=%d high=%d root_cause=%s confidence=%.2f",
        service,
        len(drifts),
        high_count,
        drift_is_root_cause,
        root_cause_confidence,
    )
    return report


def approve_rollback(report: DriftReport, approved_by: str) -> DriftReport:
    """Human approval gate — must be called before executing rollback.

    Mirrors the ``FixEngine.approve()`` pattern: changes status from
    ``"detected"`` / ``"reviewing"`` to ``"approved"``.

    Args:
        report: The :class:`DriftReport` to approve.
        approved_by: Identifier of the approving operator (e.g. "sre-alice").

    Returns:
        The same report object with ``status="approved"`` and
        ``approved_by`` set.

    Raises:
        ValueError: If the report is already rolled back or dismissed.
    """
    if report.status in ("rolled_back", "dismissed"):
        raise ValueError(
            f"Cannot approve a report that is already '{report.status}'."
        )
    report.status = "approved"
    report.approved_by = approved_by
    logger.info(
        "Drift rollback approved: service=%s approved_by=%s drifts=%d",
        report.service,
        approved_by,
        report.total_drift_count,
    )
    return report
