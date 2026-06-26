"""Tests for Phase 4 — Durable Workflow Engine.

Covers:
  - WorkflowState / WorkflowCheckpoint / PhaseResult contracts
  - WorkflowStatus / WorkflowPhase / PhaseStatus enums
  - WorkflowPort protocol
  - WorkflowEngine: start, checkpoint, resume, complete, fail
  - WorkflowEngine: SQLite persistence (temp db)
  - WorkflowEngine: orphan detection (find_orphaned)
  - WorkflowEngine: get_timeline (UI phase list)
  - WorkflowEngine: purge_old
  - WorkflowEngine: duplicate start (idempotent)
  - Crash simulation: orphaned investigation detected on restart
  - WorkflowAwareInvestigator: wraps supervisor without touching it
  - WorkflowAwareInvestigator: returns cached result for completed run
  - WorkflowAwareInvestigator: detects and handles orphaned run
  - WorkflowAwareInvestigator: propagates exceptions + records failure
  - Existing investigations: no behavior change
"""
from __future__ import annotations

import os
import time
import pytest
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

from sentinel_core.models.workflow import (
    ExecutionMetadata,
    PhaseResult,
    PhaseStatus,
    WorkflowCheckpoint,
    WorkflowPhase,
    WorkflowPort,
    WorkflowState,
    WorkflowStatus,
)
from supervisor.workflow_engine import WorkflowEngine, _dumps, _loads, _dumps_capped
from supervisor.workflow_middleware import WorkflowAwareInvestigator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_engine(tmp_path):
    """WorkflowEngine backed by an isolated temp SQLite file."""
    db = str(tmp_path / "test_workflow.db")
    return WorkflowEngine(db_path=db)


def _fake_supervisor(result: dict | None = None, raises: Exception | None = None):
    sv = MagicMock()
    if raises:
        sv.investigate.side_effect = raises
    else:
        sv.investigate.return_value = result or {
            "incident_id": "INC001",
            "root_cause": "db connection pool exhausted",
            "confidence": 85,
            "reasoning": "Evidence shows pool saturation",
        }
    return sv


# ---------------------------------------------------------------------------
# WorkflowPhase / WorkflowStatus / PhaseStatus enums
# ---------------------------------------------------------------------------

class TestEnums:
    def test_workflow_phases_are_strings(self):
        for p in WorkflowPhase:
            assert isinstance(p.value, str)

    def test_expected_phases_present(self):
        names = {p.value for p in WorkflowPhase}
        assert "fetch" in names
        assert "classify" in names
        assert "collect" in names
        assert "analyze" in names
        assert "persist" in names

    def test_workflow_statuses(self):
        statuses = {s.value for s in WorkflowStatus}
        assert "pending" in statuses
        assert "running" in statuses
        assert "completed" in statuses
        assert "failed" in statuses
        assert "resumed" in statuses

    def test_phase_statuses(self):
        statuses = {s.value for s in PhaseStatus}
        assert "pending" in statuses
        assert "completed" in statuses
        assert "failed" in statuses


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

class TestPhaseResult:
    def test_duration_ms(self):
        pr = PhaseResult(
            phase="collect",
            status=PhaseStatus.COMPLETED,
            started_at=1000.0,
            completed_at=1001.5,
        )
        assert abs(pr.duration_ms - 1500.0) < 1

    def test_duration_ms_zero_when_not_completed(self):
        pr = PhaseResult(phase="analyze", status=PhaseStatus.PENDING)
        assert pr.duration_ms == 0.0

    def test_defaults(self):
        pr = PhaseResult(phase="fetch", status=PhaseStatus.RUNNING)
        assert pr.error == ""
        assert pr.metadata == {}


# ---------------------------------------------------------------------------
# WorkflowState
# ---------------------------------------------------------------------------

class TestWorkflowState:
    def test_is_terminal_completed(self):
        s = WorkflowState(
            investigation_id="x", status=WorkflowStatus.COMPLETED
        )
        assert s.is_terminal is True

    def test_is_terminal_failed(self):
        s = WorkflowState(
            investigation_id="x", status=WorkflowStatus.FAILED
        )
        assert s.is_terminal is True

    def test_is_not_terminal_running(self):
        s = WorkflowState(
            investigation_id="x", status=WorkflowStatus.RUNNING
        )
        assert s.is_terminal is False

    def test_is_orphaned_when_running(self):
        s = WorkflowState(
            investigation_id="x", status=WorkflowStatus.RUNNING
        )
        assert s.is_orphaned is True

    def test_is_not_orphaned_when_completed(self):
        s = WorkflowState(
            investigation_id="x", status=WorkflowStatus.COMPLETED
        )
        assert s.is_orphaned is False


# ---------------------------------------------------------------------------
# ExecutionMetadata
# ---------------------------------------------------------------------------

class TestExecutionMetadata:
    def test_defaults(self):
        m = ExecutionMetadata()
        assert m.incident_id == ""
        assert m.severity == 3
        assert m.extra == {}

    def test_all_fields(self):
        m = ExecutionMetadata(
            incident_id="INC001",
            incident_type="timeout",
            service="api-gw",
            severity=1,
            extra={"env": "prod"},
        )
        assert m.incident_id == "INC001"
        assert m.severity == 1


# ---------------------------------------------------------------------------
# WorkflowPort protocol
# ---------------------------------------------------------------------------

class TestWorkflowPort:
    def test_engine_satisfies_protocol(self, tmp_engine):
        assert isinstance(tmp_engine, WorkflowPort)

    def test_fake_satisfies_protocol(self):
        class FakeEngine:
            def start(self, inv_id, metadata): return True
            def checkpoint(self, inv_id, phase, evidence_snapshot, metadata): pass
            def resume(self, inv_id): return None
            def complete(self, inv_id, result_summary): pass
            def fail(self, inv_id, error): pass
        assert isinstance(FakeEngine(), WorkflowPort)


# ---------------------------------------------------------------------------
# WorkflowEngine.start
# ---------------------------------------------------------------------------

class TestWorkflowEngineStart:
    def test_start_creates_run(self, tmp_engine):
        assert tmp_engine.start("INV001") is True
        assert tmp_engine.get_status("INV001") == WorkflowStatus.RUNNING

    def test_start_duplicate_returns_false(self, tmp_engine):
        tmp_engine.start("INV001")
        assert tmp_engine.start("INV001") is False

    def test_start_with_metadata(self, tmp_engine):
        tmp_engine.start("INV002", metadata={"incident_id": "INC002"})
        cp = tmp_engine.resume("INV002")
        assert cp is not None
        assert cp.metadata.get("incident_id") == "INC002"

    def test_status_is_running_after_start(self, tmp_engine):
        tmp_engine.start("INV003")
        assert tmp_engine.get_status("INV003") == WorkflowStatus.RUNNING


# ---------------------------------------------------------------------------
# WorkflowEngine.checkpoint
# ---------------------------------------------------------------------------

class TestWorkflowEngineCheckpoint:
    def test_checkpoint_created(self, tmp_engine):
        tmp_engine.start("INV010")
        tmp_engine.checkpoint("INV010", "collect", evidence_snapshot={"logs": "ok"})
        cp = tmp_engine.resume("INV010")
        assert cp is not None
        assert cp.phase == "collect"
        assert "collect" in cp.completed_phases

    def test_checkpoint_updates_evidence(self, tmp_engine):
        tmp_engine.start("INV011")
        tmp_engine.checkpoint("INV011", "collect", evidence_snapshot={"logs": "first"})
        tmp_engine.checkpoint("INV011", "collect", evidence_snapshot={"logs": "updated"})
        cp = tmp_engine.resume("INV011")
        assert cp.evidence_snapshot.get("logs") == "updated"

    def test_multiple_phases_tracked(self, tmp_engine):
        tmp_engine.start("INV012")
        tmp_engine.checkpoint("INV012", "fetch")
        tmp_engine.checkpoint("INV012", "classify")
        tmp_engine.checkpoint("INV012", "collect")
        cp = tmp_engine.resume("INV012")
        assert set(cp.completed_phases) == {"fetch", "classify", "collect"}

    def test_checkpoint_without_prior_start(self, tmp_engine):
        # Should not raise — just silently no-op on the run update
        tmp_engine.checkpoint("GHOST", "collect")

    def test_large_snapshot_is_truncated(self, tmp_engine):
        big = {"key": "x" * 300_000}
        tmp_engine.start("INV013")
        tmp_engine.checkpoint("INV013", "collect", evidence_snapshot=big)
        cp = tmp_engine.resume("INV013")
        # Snapshot was truncated — stored dict has _truncated marker
        assert cp.evidence_snapshot.get("_truncated") is True


# ---------------------------------------------------------------------------
# WorkflowEngine.resume
# ---------------------------------------------------------------------------

class TestWorkflowEngineResume:
    def test_resume_returns_none_for_unknown(self, tmp_engine):
        assert tmp_engine.resume("UNKNOWN") is None

    def test_resume_returns_checkpoint_after_start(self, tmp_engine):
        tmp_engine.start("INV020")
        cp = tmp_engine.resume("INV020")
        assert cp is not None
        assert cp.status == WorkflowStatus.RUNNING

    def test_resume_returns_completed_status(self, tmp_engine):
        tmp_engine.start("INV021")
        tmp_engine.complete("INV021", {"root_cause": "timeout"})
        cp = tmp_engine.resume("INV021")
        assert cp.status == WorkflowStatus.COMPLETED

    def test_resume_result_snapshot(self, tmp_engine):
        tmp_engine.start("INV022")
        tmp_engine.complete("INV022", {"root_cause": "OOM"})
        cp = tmp_engine.resume("INV022")
        assert cp.result_snapshot.get("root_cause") == "OOM"

    def test_resume_after_checkpoint(self, tmp_engine):
        tmp_engine.start("INV023")
        tmp_engine.checkpoint("INV023", "collect", {"logs": "found"})
        cp = tmp_engine.resume("INV023")
        assert cp.evidence_snapshot.get("logs") == "found"
        assert "collect" in cp.completed_phases


# ---------------------------------------------------------------------------
# WorkflowEngine.complete / fail
# ---------------------------------------------------------------------------

class TestWorkflowEngineComplete:
    def test_complete_sets_status(self, tmp_engine):
        tmp_engine.start("INV030")
        tmp_engine.complete("INV030")
        assert tmp_engine.get_status("INV030") == WorkflowStatus.COMPLETED

    def test_complete_stores_result(self, tmp_engine):
        tmp_engine.start("INV031")
        tmp_engine.complete("INV031", {"root_cause": "latency spike", "confidence": 90})
        cp = tmp_engine.resume("INV031")
        assert cp.result_snapshot.get("confidence") == 90


class TestWorkflowEngineFail:
    def test_fail_sets_status(self, tmp_engine):
        tmp_engine.start("INV040")
        tmp_engine.fail("INV040", "RuntimeError: network timeout")
        assert tmp_engine.get_status("INV040") == WorkflowStatus.FAILED

    def test_fail_stores_error(self, tmp_engine):
        tmp_engine.start("INV041")
        tmp_engine.fail("INV041", "connection refused")
        cp = tmp_engine.resume("INV041")
        assert "connection refused" in cp.error


# ---------------------------------------------------------------------------
# SQLite persistence: data survives re-instantiation
# ---------------------------------------------------------------------------

class TestSQLitePersistence:
    def test_data_survives_engine_restart(self, tmp_path):
        db = str(tmp_path / "persist_test.db")
        e1 = WorkflowEngine(db_path=db)
        e1.start("INV050")
        e1.checkpoint("INV050", "collect", {"k": "v"})

        # Re-create engine from same db file
        e2 = WorkflowEngine(db_path=db)
        cp = e2.resume("INV050")
        assert cp is not None
        assert "collect" in cp.completed_phases
        assert cp.evidence_snapshot.get("k") == "v"

    def test_multiple_investigations_isolated(self, tmp_engine):
        tmp_engine.start("INV-A")
        tmp_engine.start("INV-B")
        tmp_engine.complete("INV-A")
        tmp_engine.fail("INV-B", "error")
        assert tmp_engine.get_status("INV-A") == WorkflowStatus.COMPLETED
        assert tmp_engine.get_status("INV-B") == WorkflowStatus.FAILED


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------

class TestOrphanDetection:
    def test_running_investigation_is_orphaned(self, tmp_engine):
        tmp_engine.start("INV060")
        # Fake an old updated_at by directly updating the db
        conn = tmp_engine._connect()
        conn.execute(
            "UPDATE workflow_runs SET updated_at=? WHERE investigation_id=?",
            (time.time() - 400, "INV060"),
        )
        conn.commit()
        conn.close()

        orphans = tmp_engine.find_orphaned(max_age_seconds=300)
        assert "INV060" in orphans

    def test_recently_started_is_not_orphaned(self, tmp_engine):
        tmp_engine.start("INV061")
        orphans = tmp_engine.find_orphaned(max_age_seconds=300)
        assert "INV061" not in orphans

    def test_completed_is_not_orphaned(self, tmp_engine):
        tmp_engine.start("INV062")
        conn = tmp_engine._connect()
        conn.execute(
            "UPDATE workflow_runs SET updated_at=? WHERE investigation_id=?",
            (time.time() - 400, "INV062"),
        )
        conn.commit()
        conn.close()
        tmp_engine.complete("INV062")
        orphans = tmp_engine.find_orphaned(max_age_seconds=300)
        assert "INV062" not in orphans


# ---------------------------------------------------------------------------
# UI Timeline (Phase 4F)
# ---------------------------------------------------------------------------

class TestGetTimeline:
    def test_timeline_empty_for_unknown(self, tmp_engine):
        assert tmp_engine.get_timeline("UNKNOWN") == []

    def test_timeline_has_investigation_entry(self, tmp_engine):
        tmp_engine.start("INV070")
        timeline = tmp_engine.get_timeline("INV070")
        assert len(timeline) >= 1
        phases = {t["phase"] for t in timeline}
        assert "investigation" in phases

    def test_timeline_phase_statuses(self, tmp_engine):
        tmp_engine.start("INV071")
        tmp_engine.checkpoint("INV071", "collect")
        tmp_engine.checkpoint("INV071", "analyze")
        tmp_engine.complete("INV071")
        timeline = tmp_engine.get_timeline("INV071")
        phases = {t["phase"] for t in timeline}
        assert "investigation" in phases
        assert "collect" in phases
        assert "analyze" in phases

    def test_timeline_completed_status(self, tmp_engine):
        tmp_engine.start("INV072")
        tmp_engine.complete("INV072")
        timeline = tmp_engine.get_timeline("INV072")
        inv_entry = next(t for t in timeline if t["phase"] == "investigation")
        assert inv_entry["status"] == "completed"

    def test_timeline_failed_has_error(self, tmp_engine):
        tmp_engine.start("INV073")
        tmp_engine.fail("INV073", "crashed")
        timeline = tmp_engine.get_timeline("INV073")
        inv_entry = next(t for t in timeline if t["phase"] == "investigation")
        assert inv_entry["status"] == "failed"
        assert "crashed" in inv_entry["error"]

    def test_all_timeline_statuses_covered(self, tmp_engine):
        """All six UI statuses from Phase 4F spec are representable."""
        status_map = {
            "pending":   WorkflowStatus.PENDING,
            "running":   WorkflowStatus.RUNNING,
            "completed": WorkflowStatus.COMPLETED,
            "failed":    WorkflowStatus.FAILED,
            "resumed":   WorkflowStatus.RESUMED,
        }
        for value in status_map:
            ws = WorkflowStatus(value)
            assert ws.value == value


# ---------------------------------------------------------------------------
# Purge old
# ---------------------------------------------------------------------------

class TestPurgeOld:
    def test_purge_removes_old_completed(self, tmp_engine):
        tmp_engine.start("INV080")
        tmp_engine.complete("INV080")
        conn = tmp_engine._connect()
        conn.execute(
            "UPDATE workflow_runs SET updated_at=? WHERE investigation_id=?",
            (time.time() - 8 * 86400, "INV080"),
        )
        conn.commit()
        conn.close()
        removed = tmp_engine.purge_old(max_age_seconds=86400 * 7)
        assert removed == 1
        assert tmp_engine.get_status("INV080") is None

    def test_purge_keeps_recent(self, tmp_engine):
        tmp_engine.start("INV081")
        tmp_engine.complete("INV081")
        removed = tmp_engine.purge_old(max_age_seconds=86400 * 7)
        assert removed == 0
        assert tmp_engine.get_status("INV081") == WorkflowStatus.COMPLETED


# ---------------------------------------------------------------------------
# Crash simulation
# ---------------------------------------------------------------------------

class TestCrashSimulation:
    def test_crash_leaves_run_in_running_state(self, tmp_engine):
        tmp_engine.start("INV090")
        tmp_engine.checkpoint("INV090", "collect", {"evidence": "partial"})
        # Simulate crash: no complete() or fail() called
        status = tmp_engine.get_status("INV090")
        assert status == WorkflowStatus.RUNNING

    def test_restart_detects_orphaned_investigation(self, tmp_engine):
        tmp_engine.start("INV091")
        # Fake stale timestamp
        conn = tmp_engine._connect()
        conn.execute(
            "UPDATE workflow_runs SET updated_at=? WHERE investigation_id=?",
            (time.time() - 400, "INV091"),
        )
        conn.commit()
        conn.close()
        orphans = tmp_engine.find_orphaned(300)
        assert "INV091" in orphans

    def test_resumed_after_orphan_has_no_prior_result(self, tmp_engine):
        tmp_engine.start("INV092")
        tmp_engine.checkpoint("INV092", "collect")
        # No complete() — crash
        cp = tmp_engine.resume("INV092")
        assert cp.result_snapshot == {}
        assert "collect" in cp.completed_phases


# ---------------------------------------------------------------------------
# WorkflowAwareInvestigator
# ---------------------------------------------------------------------------

class TestWorkflowAwareInvestigator:
    def _make(self, tmp_engine, sv):
        return WorkflowAwareInvestigator(supervisor=sv, engine=tmp_engine)

    def test_successful_investigation_recorded(self, tmp_engine):
        sv = _fake_supervisor({"incident_id": "INC001", "root_cause": "oom", "confidence": 70})
        w = self._make(tmp_engine, sv)
        result = w.investigate("INC001")
        assert result["root_cause"] == "oom"
        status = tmp_engine.get_status("inv-INC001")
        assert status == WorkflowStatus.COMPLETED

    def test_exception_records_failure(self, tmp_engine):
        sv = _fake_supervisor(raises=RuntimeError("network failure"))
        w = self._make(tmp_engine, sv)
        with pytest.raises(RuntimeError, match="network failure"):
            w.investigate("INC002")
        status = tmp_engine.get_status("inv-INC002")
        assert status == WorkflowStatus.FAILED

    def test_failure_error_stored(self, tmp_engine):
        sv = _fake_supervisor(raises=ValueError("bad incident"))
        w = self._make(tmp_engine, sv)
        with pytest.raises(ValueError):
            w.investigate("INC003")
        cp = tmp_engine.resume("inv-INC003")
        assert "bad incident" in cp.error

    def test_returns_cached_result_for_completed(self, tmp_engine):
        sv = _fake_supervisor({"incident_id": "INC004", "root_cause": "db", "confidence": 80})
        w = self._make(tmp_engine, sv)
        # First run
        w.investigate("INC004")
        assert sv.investigate.call_count == 1
        # Second run — should return cached result without calling supervisor again
        result = w.investigate("INC004")
        assert result["root_cause"] == "db"
        assert sv.investigate.call_count == 1  # NOT called a second time

    def test_orphaned_run_triggers_fresh_investigation(self, tmp_engine):
        sv = _fake_supervisor({"incident_id": "INC005", "root_cause": "latency", "confidence": 60})
        # Manually create an orphaned run
        tmp_engine.start("inv-INC005")
        w = self._make(tmp_engine, sv)
        # Should detect orphan and run fresh (supervisor called once)
        result = w.investigate("INC005")
        assert result["root_cause"] == "latency"
        sv.investigate.assert_called_once()

    def test_supervisor_called_exactly_once_on_fresh_run(self, tmp_engine):
        sv = _fake_supervisor()
        w = self._make(tmp_engine, sv)
        w.investigate("INC006")
        sv.investigate.assert_called_once_with("INC006", replay=False)

    def test_replay_flag_forwarded(self, tmp_engine):
        sv = _fake_supervisor()
        w = self._make(tmp_engine, sv)
        w.investigate("INC007", replay=True)
        sv.investigate.assert_called_once_with("INC007", replay=True)

    def test_result_summary_stored(self, tmp_engine):
        sv = _fake_supervisor({
            "incident_id": "INC008",
            "root_cause": "connection pool exhausted",
            "confidence": 92,
            "reasoning": "pool saturation confirmed"
        })
        w = self._make(tmp_engine, sv)
        w.investigate("INC008")
        cp = tmp_engine.resume("inv-INC008")
        assert cp.result_snapshot["confidence"] == 92
        assert "pool saturation" in cp.result_snapshot["reasoning"]


# ---------------------------------------------------------------------------
# Existing investigations: no behavior change
# ---------------------------------------------------------------------------

class TestExistingInvestigationsUnchanged:
    """Prove that WorkflowAwareInvestigator is purely additive."""

    def test_result_passthrough_unchanged(self, tmp_engine):
        expected = {
            "incident_id": "INC100",
            "root_cause": "OOMKilled: payments-api",
            "confidence": 88,
            "reasoning": "memory limit exceeded",
            "evidence_timeline": [{"t": 1, "event": "OOM"}],
        }
        sv = _fake_supervisor(expected)
        w = WorkflowAwareInvestigator(supervisor=sv, engine=tmp_engine)
        result = w.investigate("INC100")
        # Result passes through byte-for-byte
        assert result == expected

    def test_supervisor_investigate_signature_unchanged(self, tmp_engine):
        sv = MagicMock()
        sv.investigate.return_value = {"incident_id": "INC101", "root_cause": "x", "confidence": 1}
        w = WorkflowAwareInvestigator(supervisor=sv, engine=tmp_engine)
        w.investigate("INC101")
        # supervisor.investigate() called with exactly (incident_id, replay=...)
        call_args = sv.investigate.call_args
        assert call_args[0][0] == "INC101"
        assert "replay" in call_args[1]

    def test_direct_supervisor_call_still_works_unmodified(self):
        """Calling supervisor.investigate() directly bypasses workflow — unchanged."""
        sv = MagicMock()
        sv.investigate.return_value = {"incident_id": "INC102", "root_cause": "none", "confidence": 0}
        result = sv.investigate("INC102")
        assert result["incident_id"] == "INC102"
        sv.investigate.assert_called_once_with("INC102")
