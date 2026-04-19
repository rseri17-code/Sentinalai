"""Tests for the FixEngine and ProposedFix model."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock

from supervisor.fix_engine import (
    FixEngine,
    FixStatus,
    ProposedFix,
    get_fix_engine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROLLBACK_FIX_RESULT = {
    "fix_type": "rollback",
    "fix_description": "Rollback payment-service to previous deployment",
    "immediate_action": {
        "type": "rollback",
        "command": "kubectl rollout undo deployment/payment-service",
        "service": "payment-service",
    },
    "confidence": 82.0,
    "risk_level": "low",
    "requires_approval": True,
    "incident_id": "INC0012345",
}

CODE_FIX_RESULT = {
    "fix_type": "code_fix",
    "fix_description": "Restore null check in PaymentProcessor",
    "pr_title": "fix: restore null check",
    "pr_body": "## Root cause\nRemoved null check",
    "patch": "--- a/processor.py\n+++ b/processor.py\n@@ -40 +40 @@\n+        if not payment_id:",
    "confidence": 88.0,
    "risk_level": "medium",
    "requires_approval": True,
    "repo": "myorg/payment-service",
    "sha": "abc123",
    "incident_id": "INC0012345",
}


def _make_engine():
    return FixEngine()


def _make_proposed_fix(investigation_id="inv-001", incident_id="INC0012345"):
    return ProposedFix.from_code_worker_result(
        investigation_id, incident_id, ROLLBACK_FIX_RESULT
    )


# ---------------------------------------------------------------------------
# ProposedFix model
# ---------------------------------------------------------------------------

class TestProposedFix:
    def test_from_code_worker_result_rollback(self):
        fix = ProposedFix.from_code_worker_result("inv-1", "INC001", ROLLBACK_FIX_RESULT)
        assert fix.fix_type == "rollback"
        assert fix.confidence == 82.0
        assert fix.risk_level == "low"
        assert fix.requires_approval is True
        assert fix.status == FixStatus.PROPOSED

    def test_from_code_worker_result_code_fix(self):
        fix = ProposedFix.from_code_worker_result("inv-2", "INC002", CODE_FIX_RESULT)
        assert fix.fix_type == "code_fix"
        assert fix.pr_title == "fix: restore null check"
        assert fix.patch != ""

    def test_to_dict_contains_required_fields(self):
        fix = _make_proposed_fix()
        d = fix.to_dict()
        assert "fix_id" in d
        assert "fix_type" in d
        assert "status" in d
        assert "confidence" in d
        assert "risk_level" in d
        assert "requires_approval" in d

    def test_to_dict_status_is_string(self):
        fix = _make_proposed_fix()
        d = fix.to_dict()
        assert isinstance(d["status"], str)
        assert d["status"] == "proposed"

    def test_fix_id_is_auto_generated(self):
        fix1 = _make_proposed_fix()
        fix2 = _make_proposed_fix()
        assert fix1.fix_id != fix2.fix_id


# ---------------------------------------------------------------------------
# FixEngine — propose / approve / reject
# ---------------------------------------------------------------------------

class TestFixEngineLifecycle:
    def test_propose_stores_fix(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        stored = engine.get_fix("inv-001")
        assert stored is not None
        assert stored.fix_id == fix.fix_id

    def test_propose_from_result_returns_proposed_fix(self):
        engine = _make_engine()
        fix = engine.propose_from_result("inv-001", "INC0012345", ROLLBACK_FIX_RESULT)
        assert isinstance(fix, ProposedFix)
        assert fix.fix_type == "rollback"

    def test_approve_transitions_to_approved(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        result = engine.approve("inv-001", "operator-alice")
        assert result is True
        assert fix.status == FixStatus.APPROVED
        assert fix.approved_by == "operator-alice"
        assert fix.approved_at is not None

    def test_approve_returns_false_for_missing_investigation(self):
        engine = _make_engine()
        result = engine.approve("nonexistent", "alice")
        assert result is False

    def test_approve_returns_false_when_already_approved(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        engine.approve("inv-001", "alice")
        result = engine.approve("inv-001", "bob")  # second approval
        assert result is False

    def test_reject_transitions_to_rejected(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        result = engine.reject("inv-001", "manager-bob", reason="Not enough evidence")
        assert result is True
        assert fix.status == FixStatus.REJECTED
        assert "Not enough evidence" in fix.apply_result.get("reason", "")

    def test_reject_returns_false_for_missing(self):
        engine = _make_engine()
        assert engine.reject("nonexistent", "bob") is False

    def test_get_status_returns_no_fix_when_missing(self):
        engine = _make_engine()
        status = engine.get_status("nonexistent-inv")
        assert status["status"] == "no_fix"

    def test_get_status_returns_current_status(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        status = engine.get_status("inv-001")
        assert status["status"] == "proposed"
        assert status["fix_type"] == "rollback"
        assert status["confidence"] == 82.0

    def test_mark_verified_updates_status(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        engine.mark_verified("inv-001", {"stable_readings": 3, "total_polls": 5})
        assert fix.status == FixStatus.VERIFIED

    def test_mark_failed_verification_updates_status(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        engine.mark_failed_verification("inv-001", "Timeout after 10 polls")
        assert fix.status == FixStatus.FAILED


# ---------------------------------------------------------------------------
# FixEngine.apply_fix — async
# ---------------------------------------------------------------------------

class TestApplyFix:
    def _make_mock_workers(self, rollback_success=True):
        devops = MagicMock()
        devops.execute.return_value = {
            "rollback": {
                "status": "success" if rollback_success else "failed",
                "previous_revision": "1",
                "deployment": "payment-service",
            }
        }
        itsm = MagicMock()
        itsm.execute.return_value = {"updated": {"state": "resolved"}}
        return devops, itsm

    def test_apply_raises_when_not_approved(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        devops, itsm = self._make_mock_workers()

        with pytest.raises(ValueError, match="APPROVED"):
            asyncio.get_event_loop().run_until_complete(
                engine.apply_fix("inv-001", "alice", devops, itsm)
            )

    def test_apply_raises_when_no_fix(self):
        engine = _make_engine()
        devops, itsm = self._make_mock_workers()
        with pytest.raises(ValueError):
            asyncio.get_event_loop().run_until_complete(
                engine.apply_fix("nonexistent", "alice", devops, itsm)
            )

    def test_apply_rollback_success(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        engine.approve("inv-001", "alice")
        devops, itsm = self._make_mock_workers(rollback_success=True)

        application = asyncio.get_event_loop().run_until_complete(
            engine.apply_fix("inv-001", "alice", devops, itsm)
        )
        assert application.action_taken == "rollback"
        assert application.success is True
        assert fix.status == FixStatus.APPLIED

    def test_apply_transitions_to_applied_on_success(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        engine.approve("inv-001", "alice")
        devops, itsm = self._make_mock_workers()

        asyncio.get_event_loop().run_until_complete(
            engine.apply_fix("inv-001", "alice", devops, itsm)
        )
        assert fix.status == FixStatus.APPLIED
        assert fix.applied_by == "alice"
        assert fix.applied_at is not None

    def test_apply_transitions_to_failed_on_exception(self):
        engine = _make_engine()
        fix = _make_proposed_fix()
        engine.propose("inv-001", fix)
        engine.approve("inv-001", "alice")

        devops = MagicMock()
        devops.execute.side_effect = RuntimeError("kubernetes unreachable")
        itsm = MagicMock()

        application = asyncio.get_event_loop().run_until_complete(
            engine.apply_fix("inv-001", "alice", devops, itsm)
        )
        assert application.success is False
        assert fix.status == FixStatus.FAILED

    def test_apply_code_fix_calls_create_fix_pr(self):
        engine = _make_engine()
        fix = ProposedFix.from_code_worker_result("inv-002", "INC002", CODE_FIX_RESULT)
        engine.propose("inv-002", fix)
        engine.approve("inv-002", "alice")

        devops = MagicMock()
        devops.execute.return_value = {"pr": {"number": 42, "html_url": "https://github.com/..."}}
        itsm = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            engine.apply_fix("inv-002", "alice", devops, itsm)
        )
        # Verify create_fix_pr was called
        devops.execute.assert_called_once()
        call_action = devops.execute.call_args[0][0]
        assert call_action == "create_fix_pr"


# ---------------------------------------------------------------------------
# get_fix_engine singleton
# ---------------------------------------------------------------------------

class TestGetFixEngine:
    def test_returns_same_instance_on_repeated_calls(self):
        e1 = get_fix_engine()
        e2 = get_fix_engine()
        assert e1 is e2
