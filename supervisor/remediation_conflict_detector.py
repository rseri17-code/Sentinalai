"""Remediation Conflict Detector — prevents concurrent fix collisions.

Before applying a fix, check whether another SRE is already applying a
conflicting fix to the same service.  Concurrent fix conflicts cause more
incidents than they prevent.

Example: "SRE-A is restarting pods.  Your config change will conflict — their
pods will get the old config."

Human-in-the-loop gate (mirrors fix_engine.py pattern):
  Any HIGH or BLOCKING conflict sets human_action_required=True and
  safe_to_proceed=False.  An operator must explicitly resolve the conflict
  before the proposed fix may be applied.

Usage::

    from supervisor.remediation_conflict_detector import check_conflicts

    result = check_conflicts(
        proposed_fix={
            "fix_id": "fix-001",
            "service": "payment-service",
            "fix_type": "config_change",
            "config_keys": ["replicas", "resources.limits.memory"],
            "engineer": "sre-alice",
        },
        active_fixes=[...],  # fixes currently in APPROVED/APPLYING state
    )
    if result.safe_to_proceed:
        # hand off to fix_engine.apply_fix()
        ...
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("sentinalai.remediation_conflict_detector")

# ---------------------------------------------------------------------------
# Enums & Data-classes
# ---------------------------------------------------------------------------


class ConflictType(str, Enum):
    SAME_SERVICE = "same_service"               # both touch same service
    CONFIG_KEY_OVERLAP = "config_key_overlap"   # both modify same config key
    EXECUTION_ORDERING = "execution_ordering"   # B must run after A completes
    RESOURCE_COMPETITION = "resource_competition"  # both need same K8s quota
    ROLLBACK_CONFLICT = "rollback_conflict"     # one rolls forward, other rolls back


class ConflictSeverity(str, Enum):
    LOW = "low"           # warn but proceed
    MEDIUM = "medium"     # recommend coordinating
    HIGH = "high"         # do NOT proceed without human confirmation
    BLOCKING = "blocking" # cannot proceed safely — must wait or cancel


@dataclass
class ConflictWarning:
    conflict_type: ConflictType
    severity: ConflictSeverity
    proposed_fix_id: str
    conflicting_fix_id: str
    conflicting_engineer: str    # who owns the conflicting fix
    description: str             # human-readable conflict description
    suggested_resolution: str    # what to do about it
    requires_human_decision: bool  # always True for HIGH/BLOCKING

    # Can auto-resolve?
    auto_resolvable: bool        # True only for LOW severity with clear ordering
    auto_resolution: str | None  # e.g. "Wait 2 minutes for conflicting fix to complete"


@dataclass
class ConflictCheckResult:
    proposed_fix_id: str
    proposed_fix_service: str
    proposed_fix_type: str
    conflicts: list[ConflictWarning]
    has_blocking_conflict: bool
    has_high_conflict: bool
    safe_to_proceed: bool      # True only if no HIGH/BLOCKING conflicts
    human_action_required: bool
    summary: str               # concise human-readable overview


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

_ROLLBACK_TYPES = frozenset({"rollback", "rollback_deployment", "iac_rollback"})
_FORWARD_DEPLOY_TYPES = frozenset({"deploy", "deployment", "scale_up", "code_fix", "config_change"})
_POD_RESTART_TYPES = frozenset({"pod_restart", "restart", "rolling_restart"})

_HUMAN_REQUIRED_SEVERITIES = frozenset({ConflictSeverity.HIGH, ConflictSeverity.BLOCKING})


def _same_service(proposed: dict, active: dict) -> bool:
    return proposed.get("service") == active.get("service")


def _overlapping_keys(proposed: dict, active: dict) -> list[str]:
    p_keys = set(proposed.get("config_keys") or [])
    a_keys = set(active.get("config_keys") or [])
    return sorted(p_keys & a_keys)


def _is_rollback(fix: dict) -> bool:
    return fix.get("fix_type", "").lower() in _ROLLBACK_TYPES


def _is_forward_deploy(fix: dict) -> bool:
    return fix.get("fix_type", "").lower() in _FORWARD_DEPLOY_TYPES


def _is_pod_restart(fix: dict) -> bool:
    return fix.get("fix_type", "").lower() in _POD_RESTART_TYPES


def _is_config_change(fix: dict) -> bool:
    return fix.get("fix_type", "").lower() in {"config_change", "configmap_update"}


# ---------------------------------------------------------------------------
# Individual conflict detectors
# ---------------------------------------------------------------------------


def _detect_same_service_conflict(
    proposed: dict, active: dict
) -> ConflictWarning | None:
    """Detect when both fixes target the same service.

    Severity rules:
    - Both are rollbacks of the same service → HIGH (double-rollback confusion)
    - Otherwise → MEDIUM (general coordination needed)
    """
    if not _same_service(proposed, active):
        return None

    prop_id = proposed.get("fix_id", "unknown")
    act_id = active.get("fix_id", "unknown")
    act_engineer = active.get("engineer", "unknown-engineer")
    service = proposed.get("service", "unknown-service")

    both_rollbacks = _is_rollback(proposed) and _is_rollback(active)
    severity = ConflictSeverity.HIGH if both_rollbacks else ConflictSeverity.MEDIUM

    if both_rollbacks:
        description = (
            f"Both fixes are rollbacks targeting '{service}'. "
            f"Concurrent rollbacks can leave the service in an inconsistent state "
            f"and make it impossible to determine which version is authoritative."
        )
        resolution = (
            f"Coordinate with {act_engineer} to decide which rollback to execute. "
            "Cancel one fix before proceeding."
        )
    else:
        description = (
            f"Both fixes target service '{service}'. "
            f"{act_engineer}'s fix ({act_id}) is currently in progress."
        )
        resolution = (
            f"Coordinate with {act_engineer} before applying this fix. "
            "Consider waiting for their fix to complete and verify."
        )

    return ConflictWarning(
        conflict_type=ConflictType.SAME_SERVICE,
        severity=severity,
        proposed_fix_id=prop_id,
        conflicting_fix_id=act_id,
        conflicting_engineer=act_engineer,
        description=description,
        suggested_resolution=resolution,
        requires_human_decision=(severity in _HUMAN_REQUIRED_SEVERITIES),
        auto_resolvable=False,
        auto_resolution=None,
    )


def _detect_config_key_conflict(
    proposed: dict, active: dict
) -> ConflictWarning | None:
    """Detect when both fixes modify the same config key(s).

    Severity: HIGH — two concurrent writes to the same key will produce a
    race condition; the last writer wins and the first SRE's intent is silently
    overwritten.
    """
    if not _same_service(proposed, active):
        return None

    shared_keys = _overlapping_keys(proposed, active)
    if not shared_keys:
        return None

    prop_id = proposed.get("fix_id", "unknown")
    act_id = active.get("fix_id", "unknown")
    act_engineer = active.get("engineer", "unknown-engineer")
    service = proposed.get("service", "unknown-service")

    keys_str = ", ".join(f"'{k}'" for k in shared_keys[:5])
    if len(shared_keys) > 5:
        keys_str += f" … (+{len(shared_keys) - 5} more)"

    description = (
        f"Both fixes modify the same config key(s) on '{service}': {keys_str}. "
        f"Concurrent modifications will race — the last write wins and the other "
        f"SRE's intent will be silently overwritten."
    )
    resolution = (
        f"Coordinate with {act_engineer} ({act_id}). "
        "Merge both changes into a single fix, or wait for theirs to complete and verify."
    )

    return ConflictWarning(
        conflict_type=ConflictType.CONFIG_KEY_OVERLAP,
        severity=ConflictSeverity.HIGH,
        proposed_fix_id=prop_id,
        conflicting_fix_id=act_id,
        conflicting_engineer=act_engineer,
        description=description,
        suggested_resolution=resolution,
        requires_human_decision=True,
        auto_resolvable=False,
        auto_resolution=None,
    )


def _detect_ordering_conflict(
    proposed: dict, active: dict
) -> ConflictWarning | None:
    """Detect execution ordering conflicts on the same service.

    Rule: if the active fix is a pod restart and the proposed fix is a config
    change on the same service, the config change must wait — pods started
    during the restart will pick up the new config, but pods started before the
    restart will still have the old config, causing a mixed-config fleet.

    Severity: MEDIUM — coordination is needed but the fix is not inherently
    destructive if ordered correctly.
    """
    if not _same_service(proposed, active):
        return None

    active_is_restart = _is_pod_restart(active)
    proposed_is_config = _is_config_change(proposed)

    if not (active_is_restart and proposed_is_config):
        return None

    prop_id = proposed.get("fix_id", "unknown")
    act_id = active.get("fix_id", "unknown")
    act_engineer = active.get("engineer", "unknown-engineer")
    service = proposed.get("service", "unknown-service")

    description = (
        f"{act_engineer} is currently restarting pods on '{service}' ({act_id}). "
        f"Applying a config change now will result in a mixed-config fleet: "
        "pods started before the restart will have the old config, pods started "
        "after will have the new config."
    )
    resolution = (
        f"Wait for {act_engineer}'s pod restart ({act_id}) to complete before "
        "applying the config change. This ensures all pods start with the new config."
    )

    # Estimate wait time: pod restarts typically complete in 1-3 minutes
    auto_resolution = (
        "Wait for the active pod restart to complete (typically 1-3 minutes), "
        "then re-submit this fix."
    )

    return ConflictWarning(
        conflict_type=ConflictType.EXECUTION_ORDERING,
        severity=ConflictSeverity.MEDIUM,
        proposed_fix_id=prop_id,
        conflicting_fix_id=act_id,
        conflicting_engineer=act_engineer,
        description=description,
        suggested_resolution=resolution,
        requires_human_decision=False,
        auto_resolvable=True,
        auto_resolution=auto_resolution,
    )


def _detect_rollback_conflict(
    proposed: dict, active: dict
) -> ConflictWarning | None:
    """Detect when one fix rolls back while the other deploys forward.

    This is the most dangerous conflict: if SRE-A rolls back to v1.9 while
    SRE-B deploys v2.1, the final state depends on execution order and may
    leave the service in an undefined version.

    Severity: BLOCKING — cannot proceed safely.
    """
    if not _same_service(proposed, active):
        return None

    prop_rollback = _is_rollback(proposed)
    act_rollback = _is_rollback(active)
    prop_forward = _is_forward_deploy(proposed)
    act_forward = _is_forward_deploy(active)

    # Conflict only when one is rollback and the other is forward deploy
    conflict = (prop_rollback and act_forward) or (prop_forward and act_rollback)
    if not conflict:
        return None

    prop_id = proposed.get("fix_id", "unknown")
    act_id = active.get("fix_id", "unknown")
    act_engineer = active.get("engineer", "unknown-engineer")
    service = proposed.get("service", "unknown-service")

    if prop_rollback:
        direction_msg = (
            f"You are rolling back '{service}', but {act_engineer} ({act_id}) "
            "is currently deploying a forward change to the same service."
        )
    else:
        direction_msg = (
            f"You are deploying a forward change to '{service}', but "
            f"{act_engineer} ({act_id}) is currently rolling it back."
        )

    description = (
        f"{direction_msg} "
        "Applying both fixes concurrently will leave the service in an undefined "
        "version state — the final version depends entirely on execution order."
    )
    resolution = (
        "STOP. Coordinate with the team to decide the intended version. "
        "Only one fix should proceed. Cancel the other fix before applying either."
    )

    return ConflictWarning(
        conflict_type=ConflictType.ROLLBACK_CONFLICT,
        severity=ConflictSeverity.BLOCKING,
        proposed_fix_id=prop_id,
        conflicting_fix_id=act_id,
        conflicting_engineer=act_engineer,
        description=description,
        suggested_resolution=resolution,
        requires_human_decision=True,
        auto_resolvable=False,
        auto_resolution=None,
    )


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _build_summary(
    conflicts: list[ConflictWarning],
    safe_to_proceed: bool,
) -> str:
    if not conflicts:
        return "No conflicts detected. Safe to proceed."

    blocking = sum(1 for c in conflicts if c.severity == ConflictSeverity.BLOCKING)
    high = sum(1 for c in conflicts if c.severity == ConflictSeverity.HIGH)
    medium = sum(1 for c in conflicts if c.severity == ConflictSeverity.MEDIUM)
    low = sum(1 for c in conflicts if c.severity == ConflictSeverity.LOW)

    parts: list[str] = []
    if blocking:
        parts.append(f"{blocking} BLOCKING")
    if high:
        parts.append(f"{high} HIGH")
    if medium:
        parts.append(f"{medium} MEDIUM")
    if low:
        parts.append(f"{low} LOW")

    severity_str = ", ".join(parts)
    total = len(conflicts)
    noun = "conflict" if total == 1 else "conflicts"

    engineers = sorted(
        {c.conflicting_engineer for c in conflicts if c.conflicting_engineer != "unknown-engineer"}
    )
    engineer_str = (
        f" Coordinate with: {', '.join(engineers)}." if engineers else ""
    )

    if safe_to_proceed:
        action = " Review warnings before proceeding."
    else:
        action = " Do NOT proceed without human resolution."

    return (
        f"{total} {noun} detected: {severity_str}.{engineer_str}{action}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_conflicts(
    proposed_fix: dict[str, Any],
    active_fixes: list[dict[str, Any]],
) -> ConflictCheckResult:
    """Check if *proposed_fix* conflicts with any currently active fixes.

    Args:
        proposed_fix: Dict describing the fix being proposed.
            Expected keys: ``fix_id``, ``service``, ``fix_type``,
            ``config_keys`` (list of str), ``engineer``.
        active_fixes: List of fix dicts currently in APPROVED or APPLYING
            state.  Same key schema as *proposed_fix*.

    Returns:
        A :class:`ConflictCheckResult` with all detected conflicts and a
        consolidated verdict.
    """
    conflicts: list[ConflictWarning] = []

    detectors = [
        _detect_rollback_conflict,   # check most severe first
        _detect_config_key_conflict,
        _detect_same_service_conflict,
        _detect_ordering_conflict,
    ]

    # Track which (proposed, active) pairs have already produced a conflict so
    # we don't double-count for the same pair from different detectors.
    # We still want to record all distinct ConflictTypes though.
    for active in active_fixes:
        for detector in detectors:
            warning = detector(proposed_fix, active)
            if warning is not None:
                conflicts.append(warning)

    has_blocking = any(c.severity == ConflictSeverity.BLOCKING for c in conflicts)
    has_high = any(c.severity == ConflictSeverity.HIGH for c in conflicts)
    safe_to_proceed = not (has_blocking or has_high)
    human_required = not safe_to_proceed

    summary = _build_summary(conflicts, safe_to_proceed)

    result = ConflictCheckResult(
        proposed_fix_id=proposed_fix.get("fix_id", "unknown"),
        proposed_fix_service=proposed_fix.get("service", "unknown"),
        proposed_fix_type=proposed_fix.get("fix_type", "unknown"),
        conflicts=conflicts,
        has_blocking_conflict=has_blocking,
        has_high_conflict=has_high,
        safe_to_proceed=safe_to_proceed,
        human_action_required=human_required,
        summary=summary,
    )

    logger.info(
        "Conflict check: fix=%s service=%s total_conflicts=%d blocking=%s high=%s safe=%s",
        proposed_fix.get("fix_id"),
        proposed_fix.get("service"),
        len(conflicts),
        has_blocking,
        has_high,
        safe_to_proceed,
    )
    return result
