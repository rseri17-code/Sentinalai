"""Tests for supervisor receipt collection and replay integration."""

import json
import tempfile
from pathlib import Path

import pytest

from supervisor.agent import SentinalAISupervisor
from supervisor.replay import ReplayStore


class TestSupervisorReceipts:
    """Verify the supervisor collects receipts during investigation."""

    def test_investigation_with_replay_store(self, tmp_path):
        """Supervisor should persist receipts when replay_dir is set."""
        sup = SentinalAISupervisor(replay_dir=str(tmp_path))
        result = sup.investigate("INC12345")
        assert result["confidence"] > 0

        # Check that replay artifact was saved
        store = ReplayStore(str(tmp_path))
        artifact = store.load("INC12345")
        assert artifact is not None
        assert len(artifact["receipts"]) > 0
        assert artifact["result"]["root_cause"] == result["root_cause"]

    def test_receipts_have_required_fields(self, tmp_path):
        """Each receipt must have tool, action, status, timestamps."""
        sup = SentinalAISupervisor(replay_dir=str(tmp_path))
        sup.investigate("INC12345")

        store = ReplayStore(str(tmp_path))
        artifact = store.load("INC12345")
        for receipt in artifact["receipts"]:
            assert "tool" in receipt
            assert "action" in receipt
            assert receipt["status"] in ("success", "error", "timeout")
            assert receipt["elapsed_ms"] >= 0
            assert len(receipt["correlation_id"]) == 12

    def test_replay_returns_same_result(self, tmp_path):
        """Replay must return identical result to original investigation."""
        sup = SentinalAISupervisor(replay_dir=str(tmp_path))
        original = sup.investigate("INC12345")
        replayed = sup.investigate("INC12345", replay=True)
        assert replayed["root_cause"] == original["root_cause"]
        assert replayed["confidence"] == original["confidence"]

    def test_replay_without_artifact_runs_fresh(self, tmp_path):
        """When no artifact exists, replay mode should run a fresh investigation."""
        sup = SentinalAISupervisor(replay_dir=str(tmp_path))
        result = sup.investigate("INC12345", replay=True)
        # Falls through to fresh investigation since no artifact exists
        assert result["confidence"] > 0

    def test_determinism_all_incidents(self, tmp_path):
        """All 10 incidents should produce deterministic results via receipts."""
        incident_ids = [
            "INC12345", "INC12346", "INC12347", "INC12348", "INC12349",
            "INC12350", "INC12351", "INC12352", "INC12353", "INC12354",
        ]
        sup = SentinalAISupervisor(replay_dir=str(tmp_path))

        for iid in incident_ids:
            r1 = sup.investigate(iid)
            r2 = sup.investigate(iid, replay=True)
            assert r1["root_cause"] == r2["root_cause"], f"Determinism failure for {iid}"
            assert r1["confidence"] == r2["confidence"], f"Confidence mismatch for {iid}"


class TestSupervisorBudget:
    """Verify budget enforcement in the supervisor."""

    def test_investigation_completes_within_budget(self, tmp_path):
        """An investigation should not exceed MAX_TOOL_CALLS_PER_CASE."""
        sup = SentinalAISupervisor(replay_dir=str(tmp_path))
        sup.investigate("INC12345")

        store = ReplayStore(str(tmp_path))
        artifact = store.load("INC12345")
        # Budget is 20 calls max; typical investigation uses 5-8
        assert len(artifact["receipts"]) <= 20
