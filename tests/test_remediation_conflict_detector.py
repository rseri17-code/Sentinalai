"""Tests for supervisor/remediation_conflict_detector.py

Coverage:
- no conflicts → safe_to_proceed=True, human_action_required=False
- same-service conflict → MEDIUM severity
- same-service conflict (both rollbacks) → HIGH severity
- config key overlap → HIGH severity
- rollback vs forward deploy → BLOCKING severity
- ordering conflict (pod restart + config change) → MEDIUM, auto_resolvable=True
- multiple simultaneous conflicts
- auto_resolvable LOW conflict (basic test via LOW severity path)
- human_action_required for HIGH conflict
- human_action_required for BLOCKING conflict
- summary string format
- ConflictCheckResult fields
"""
from __future__ import annotations

import pytest

from supervisor.remediation_conflict_detector import (
    ConflictCheckResult,
    ConflictSeverity,
    ConflictType,
    ConflictWarning,
    check_conflicts,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

def _fix(
    fix_id: str = "fix-001",
    service: str = "payment-service",
    fix_type: str = "config_change",
    config_keys: list[str] | None = None,
    engineer: str = "sre-alice",
) -> dict:
    return {
        "fix_id": fix_id,
        "service": service,
        "fix_type": fix_type,
        "config_keys": config_keys or [],
        "engineer": engineer,
    }


# ---------------------------------------------------------------------------
# No conflicts
# ---------------------------------------------------------------------------

class TestNoConflicts:
    def test_empty_active_fixes_is_safe(self):
        result = check_conflicts(_fix(), active_fixes=[])

        assert isinstance(result, ConflictCheckResult)
        assert result.safe_to_proceed is True
        assert result.has_blocking_conflict is False
        assert result.has_high_conflict is False
        assert result.human_action_required is False
        assert result.conflicts == []

    def test_different_service_no_conflict(self):
        proposed = _fix(service="payment-service", fix_type="config_change")
        active = _fix(fix_id="fix-002", service="auth-service", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.safe_to_proceed is True
        assert len(result.conflicts) == 0

    def test_summary_when_no_conflicts(self):
        result = check_conflicts(_fix(), active_fixes=[])
        assert "No conflicts" in result.summary
        assert "Safe to proceed" in result.summary


# ---------------------------------------------------------------------------
# Same-service conflict
# ---------------------------------------------------------------------------

class TestSameServiceConflict:
    def test_same_service_different_types_is_medium(self):
        proposed = _fix(fix_type="config_change", config_keys=["timeout"])
        active = _fix(fix_id="fix-002", fix_type="scale_up", engineer="sre-bob", config_keys=["replicas"])

        result = check_conflicts(proposed, active_fixes=[active])

        same_svc_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.SAME_SERVICE]
        assert len(same_svc_conflicts) >= 1
        assert same_svc_conflicts[0].severity == ConflictSeverity.MEDIUM
        assert same_svc_conflicts[0].conflicting_engineer == "sre-bob"

    def test_same_service_is_not_blocking(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="scale_up", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.has_blocking_conflict is False

    def test_same_service_medium_means_safe_to_proceed(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="scale_up", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        # MEDIUM only → still safe to proceed (no HIGH/BLOCKING)
        assert result.safe_to_proceed is True
        assert result.human_action_required is False

    def test_both_rollbacks_same_service_is_high(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="rollback", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        same_svc_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.SAME_SERVICE]
        assert any(c.severity == ConflictSeverity.HIGH for c in same_svc_conflicts)

    def test_both_rollbacks_sets_high_conflict_flag(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="rollback", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.has_high_conflict is True
        assert result.safe_to_proceed is False
        assert result.human_action_required is True

    def test_conflict_contains_conflicting_fix_id(self):
        proposed = _fix(fix_id="fix-001", fix_type="config_change")
        active = _fix(fix_id="fix-ABC", fix_type="scale_up", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        for conflict in result.conflicts:
            assert conflict.proposed_fix_id == "fix-001"
            assert conflict.conflicting_fix_id == "fix-ABC"


# ---------------------------------------------------------------------------
# Config key overlap
# ---------------------------------------------------------------------------

class TestConfigKeyOverlap:
    def test_overlapping_keys_is_high(self):
        proposed = _fix(config_keys=["replicas", "resources.limits.memory"])
        active = _fix(
            fix_id="fix-002",
            config_keys=["resources.limits.memory", "env.TIMEOUT"],
            engineer="sre-bob",
        )

        result = check_conflicts(proposed, active_fixes=[active])

        key_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.CONFIG_KEY_OVERLAP]
        assert len(key_conflicts) == 1
        assert key_conflicts[0].severity == ConflictSeverity.HIGH

    def test_overlapping_keys_sets_high_flag(self):
        proposed = _fix(config_keys=["image"])
        active = _fix(fix_id="fix-002", config_keys=["image"], engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.has_high_conflict is True
        assert result.safe_to_proceed is False
        assert result.human_action_required is True

    def test_no_overlap_no_key_conflict(self):
        proposed = _fix(config_keys=["replicas"])
        active = _fix(fix_id="fix-002", config_keys=["image"], engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        key_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.CONFIG_KEY_OVERLAP]
        assert len(key_conflicts) == 0

    def test_empty_config_keys_no_key_conflict(self):
        proposed = _fix(config_keys=[])
        active = _fix(fix_id="fix-002", config_keys=["image"], engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        key_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.CONFIG_KEY_OVERLAP]
        assert len(key_conflicts) == 0

    def test_key_conflict_description_mentions_keys(self):
        proposed = _fix(config_keys=["resources.limits.memory"])
        active = _fix(
            fix_id="fix-002",
            config_keys=["resources.limits.memory"],
            engineer="sre-bob",
        )

        result = check_conflicts(proposed, active_fixes=[active])

        key_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.CONFIG_KEY_OVERLAP]
        assert "resources.limits.memory" in key_conflicts[0].description


# ---------------------------------------------------------------------------
# Rollback vs forward deploy → BLOCKING
# ---------------------------------------------------------------------------

class TestRollbackConflict:
    def test_rollback_vs_forward_deploy_is_blocking(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        rollback_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.ROLLBACK_CONFLICT]
        assert len(rollback_conflicts) == 1
        assert rollback_conflicts[0].severity == ConflictSeverity.BLOCKING

    def test_forward_deploy_vs_rollback_is_blocking(self):
        proposed = _fix(fix_type="deploy")
        active = _fix(fix_id="fix-002", fix_type="rollback", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        rollback_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.ROLLBACK_CONFLICT]
        assert len(rollback_conflicts) == 1
        assert rollback_conflicts[0].severity == ConflictSeverity.BLOCKING

    def test_rollback_conflict_sets_blocking_flag(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.has_blocking_conflict is True
        assert result.safe_to_proceed is False
        assert result.human_action_required is True

    def test_rollback_vs_code_fix_is_blocking(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="code_fix", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        rollback_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.ROLLBACK_CONFLICT]
        assert len(rollback_conflicts) == 1
        assert rollback_conflicts[0].severity == ConflictSeverity.BLOCKING

    def test_rollback_conflict_not_triggered_on_different_service(self):
        proposed = _fix(service="payment-service", fix_type="rollback")
        active = _fix(fix_id="fix-002", service="auth-service", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        rollback_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.ROLLBACK_CONFLICT]
        assert len(rollback_conflicts) == 0

    def test_requires_human_decision_true_for_rollback_conflict(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        rollback_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.ROLLBACK_CONFLICT]
        assert rollback_conflicts[0].requires_human_decision is True


# ---------------------------------------------------------------------------
# Ordering conflict (pod restart + config change)
# ---------------------------------------------------------------------------

class TestOrderingConflict:
    def test_pod_restart_active_plus_config_change_proposed(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="pod_restart", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        ordering_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.EXECUTION_ORDERING]
        assert len(ordering_conflicts) == 1
        assert ordering_conflicts[0].severity == ConflictSeverity.MEDIUM

    def test_ordering_conflict_is_auto_resolvable(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="restart", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        ordering_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.EXECUTION_ORDERING]
        assert len(ordering_conflicts) == 1
        assert ordering_conflicts[0].auto_resolvable is True
        assert ordering_conflicts[0].auto_resolution is not None
        assert len(ordering_conflicts[0].auto_resolution) > 0

    def test_ordering_conflict_does_not_block(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="pod_restart", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        # MEDIUM ordering conflict — still safe to proceed
        assert result.has_blocking_conflict is False
        # May or may not have high conflict from same_service; key: no blocking
        ordering_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.EXECUTION_ORDERING]
        assert ordering_conflicts[0].severity == ConflictSeverity.MEDIUM

    def test_ordering_conflict_requires_human_decision_false(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="pod_restart", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        ordering_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.EXECUTION_ORDERING]
        # MEDIUM → no human decision required (auto_resolvable)
        assert ordering_conflicts[0].requires_human_decision is False

    def test_config_change_active_plus_pod_restart_proposed_no_ordering(self):
        """The ordering rule is directional: pod_restart active → config_change proposed."""
        proposed = _fix(fix_type="pod_restart")
        active = _fix(fix_id="fix-002", fix_type="config_change", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        ordering_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.EXECUTION_ORDERING]
        assert len(ordering_conflicts) == 0


# ---------------------------------------------------------------------------
# Multiple simultaneous conflicts
# ---------------------------------------------------------------------------

class TestMultipleConflicts:
    def test_two_active_fixes_both_conflicting(self):
        proposed = _fix(fix_id="fix-001", fix_type="rollback", config_keys=["image"])
        active_1 = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob", config_keys=["image"])
        active_2 = _fix(fix_id="fix-003", fix_type="rollback", engineer="sre-charlie", config_keys=["replicas"])

        result = check_conflicts(proposed, active_fixes=[active_1, active_2])

        assert len(result.conflicts) >= 2
        assert result.has_blocking_conflict is True  # rollback vs deploy

    def test_safe_to_proceed_false_with_multiple_conflicts(self):
        proposed = _fix(fix_type="rollback", config_keys=["image"])
        active_1 = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")
        active_2 = _fix(fix_id="fix-003", fix_type="config_change", engineer="sre-charlie", config_keys=["image"])

        result = check_conflicts(proposed, active_fixes=[active_1, active_2])

        assert result.safe_to_proceed is False
        assert result.human_action_required is True

    def test_summary_reflects_multiple_severities(self):
        proposed = _fix(fix_type="rollback")
        active_1 = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active_1])

        assert "BLOCKING" in result.summary

    def test_result_contains_all_conflict_types_for_complex_scenario(self):
        proposed = _fix(
            fix_id="fix-001",
            fix_type="config_change",
            config_keys=["resources.limits.memory"],
        )
        # active_1: same service, pod_restart → ordering conflict + same service
        active_1 = _fix(
            fix_id="fix-002",
            fix_type="pod_restart",
            engineer="sre-bob",
            config_keys=[],
        )
        # active_2: same service, same config key → key overlap + same service
        active_2 = _fix(
            fix_id="fix-003",
            fix_type="scale_up",
            engineer="sre-charlie",
            config_keys=["resources.limits.memory"],
        )

        result = check_conflicts(proposed, active_fixes=[active_1, active_2])

        conflict_types = {c.conflict_type for c in result.conflicts}
        assert ConflictType.EXECUTION_ORDERING in conflict_types
        assert ConflictType.CONFIG_KEY_OVERLAP in conflict_types
        assert ConflictType.SAME_SERVICE in conflict_types


# ---------------------------------------------------------------------------
# ConflictCheckResult field validation
# ---------------------------------------------------------------------------

class TestConflictCheckResultFields:
    def test_proposed_fix_id_propagated(self):
        proposed = _fix(fix_id="my-fix-99")
        result = check_conflicts(proposed, active_fixes=[])
        assert result.proposed_fix_id == "my-fix-99"

    def test_proposed_fix_service_propagated(self):
        proposed = _fix(service="auth-service")
        result = check_conflicts(proposed, active_fixes=[])
        assert result.proposed_fix_service == "auth-service"

    def test_proposed_fix_type_propagated(self):
        proposed = _fix(fix_type="rollback")
        result = check_conflicts(proposed, active_fixes=[])
        assert result.proposed_fix_type == "rollback"

    def test_conflicts_list_type(self):
        result = check_conflicts(_fix(), active_fixes=[])
        assert isinstance(result.conflicts, list)


# ---------------------------------------------------------------------------
# human_action_required for HIGH conflicts
# ---------------------------------------------------------------------------

class TestHumanActionRequired:
    def test_high_conflict_sets_human_action_required(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="rollback", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.has_high_conflict is True
        assert result.human_action_required is True

    def test_blocking_conflict_sets_human_action_required(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert result.has_blocking_conflict is True
        assert result.human_action_required is True

    def test_medium_only_does_not_set_human_action_required(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="scale_up", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        # Should only have MEDIUM/LOW conflicts
        assert not result.has_high_conflict
        assert not result.has_blocking_conflict
        assert result.human_action_required is False


# ---------------------------------------------------------------------------
# Summary string format
# ---------------------------------------------------------------------------

class TestSummaryFormat:
    def test_summary_with_blocking(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert "BLOCKING" in result.summary
        assert "conflict" in result.summary.lower()

    def test_summary_with_no_conflicts(self):
        result = check_conflicts(_fix(), active_fixes=[])
        assert "No conflicts" in result.summary

    def test_summary_mentions_engineer_when_known(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob-jones")

        result = check_conflicts(proposed, active_fixes=[active])

        # Engineer name should appear in summary
        assert "sre-bob-jones" in result.summary

    def test_summary_with_high_conflict(self):
        proposed = _fix(config_keys=["image"])
        active = _fix(fix_id="fix-002", config_keys=["image"], engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        assert "HIGH" in result.summary


# ---------------------------------------------------------------------------
# ConflictWarning auto_resolvable for LOW / MEDIUM scenarios
# ---------------------------------------------------------------------------

class TestAutoResolvable:
    def test_ordering_conflict_is_auto_resolvable(self):
        proposed = _fix(fix_type="config_change")
        active = _fix(fix_id="fix-002", fix_type="pod_restart", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        ordering_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.EXECUTION_ORDERING]
        assert ordering_conflicts[0].auto_resolvable is True
        assert ordering_conflicts[0].auto_resolution is not None

    def test_blocking_conflict_is_not_auto_resolvable(self):
        proposed = _fix(fix_type="rollback")
        active = _fix(fix_id="fix-002", fix_type="deploy", engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        rollback_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.ROLLBACK_CONFLICT]
        assert rollback_conflicts[0].auto_resolvable is False
        assert rollback_conflicts[0].auto_resolution is None

    def test_high_config_key_conflict_is_not_auto_resolvable(self):
        proposed = _fix(config_keys=["image"])
        active = _fix(fix_id="fix-002", config_keys=["image"], engineer="sre-bob")

        result = check_conflicts(proposed, active_fixes=[active])

        key_conflicts = [c for c in result.conflicts if c.conflict_type == ConflictType.CONFIG_KEY_OVERLAP]
        assert key_conflicts[0].auto_resolvable is False
