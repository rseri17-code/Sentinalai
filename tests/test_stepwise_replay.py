"""Tests for stepwise replay and ReplayStore.list_all()."""

from __future__ import annotations

import tempfile

import pytest

from supervisor.replay import ReplayStore, ReplayStep, replay_stepwise


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RECEIPTS = [
    {"worker": "ops_worker",     "action": "get_incident_by_id", "params": {"incident_id": "INC001"}, "status": "success", "elapsed_ms": 50},
    {"worker": "log_worker",     "action": "search_logs",         "params": {"service": "api"},       "status": "success", "elapsed_ms": 120},
    {"worker": "apm_worker",     "action": "get_golden_signals",  "params": {"service": "api"},       "status": "success", "elapsed_ms": 80},
    {"worker": "metrics_worker", "action": "query_metrics",       "params": {"service": "api"},       "status": "success", "elapsed_ms": 60},
]

EVIDENCE = {
    "get_incident_by_id": {"incident_id": "INC001", "summary": "API errors"},
    "search_logs":        {"logs": [{"msg": "connection refused"}]},
    "get_golden_signals": {"golden_signals": {"error_rate": 0.4}},
    "query_metrics":      {"metrics": {"cpu": 90}},
}

RESULT = {
    "root_cause": "Connection pool exhaustion",
    "confidence": 82,
    "reasoning": "High error rate + full connection pool",
    "evidence_timeline": [],
}


@pytest.fixture
def replay_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def store_with_artifact(replay_dir):
    store = ReplayStore(replay_dir)
    store.save("INC001", RECEIPTS, RESULT, EVIDENCE)
    return store, replay_dir


# ---------------------------------------------------------------------------
# ReplayStore.list_all
# ---------------------------------------------------------------------------

class TestListAll:
    def test_empty_dir_returns_empty(self, replay_dir):
        store = ReplayStore(replay_dir)
        assert store.list_all() == []

    def test_lists_saved_artifacts(self, store_with_artifact):
        store, replay_dir = store_with_artifact
        entries = store.list_all()
        assert len(entries) == 1
        assert entries[0]["case_id"] == "INC001"
        assert entries[0]["receipt_count"] == len(RECEIPTS)
        assert entries[0]["confidence"] == 82

    def test_multiple_cases(self, replay_dir):
        store = ReplayStore(replay_dir)
        store.save("INC001", RECEIPTS[:2], RESULT, EVIDENCE)
        store.save("INC002", RECEIPTS[:1], RESULT, EVIDENCE)
        entries = store.list_all()
        case_ids = {e["case_id"] for e in entries}
        assert "INC001" in case_ids
        assert "INC002" in case_ids

    def test_nonexistent_dir_returns_empty(self):
        store = ReplayStore("/tmp/does_not_exist_xyz_sentinalai")
        assert store.list_all() == []


# ---------------------------------------------------------------------------
# replay_stepwise — basic mechanics
# ---------------------------------------------------------------------------

class TestReplayStepwise:
    def test_yields_one_step_per_receipt(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        assert len(steps) == len(RECEIPTS)

    def test_step_numbers_are_sequential(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        assert [s.step_num for s in steps] == list(range(1, len(RECEIPTS) + 1))

    def test_total_steps_correct(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        for step in steps:
            assert step.total_steps == len(RECEIPTS)

    def test_receipt_preserved(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        assert steps[0].receipt["action"] == "get_incident_by_id"
        assert steps[1].receipt["action"] == "search_logs"

    def test_evidence_accumulates(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        # Step 1: only get_incident_by_id evidence
        assert "get_incident_by_id" in steps[0].evidence_snapshot
        assert "search_logs" not in steps[0].evidence_snapshot
        # Step 2: logs added
        assert "search_logs" in steps[1].evidence_snapshot
        # Final step: all evidence present
        final = steps[-1].evidence_snapshot
        assert "get_golden_signals" in final
        assert "query_metrics" in final

    def test_evidence_snapshots_are_independent_copies(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        # Modifying a later snapshot doesn't retroactively change an earlier one
        steps[-1].evidence_snapshot["injected"] = True
        assert "injected" not in steps[0].evidence_snapshot

    def test_raises_on_missing_artifact(self, replay_dir):
        with pytest.raises(ValueError, match="No replay artifact"):
            list(replay_stepwise("INC_NOTEXIST", replay_dir))

    def test_empty_receipts_yields_nothing(self, replay_dir):
        store = ReplayStore(replay_dir)
        store.save("INC_EMPTY", [], RESULT, EVIDENCE)
        steps = list(replay_stepwise("INC_EMPTY", replay_dir))
        assert steps == []


# ---------------------------------------------------------------------------
# replay_stepwise — with analyze_fn
# ---------------------------------------------------------------------------

class TestReplayStepwiseWithAnalyzeFn:
    def test_partial_result_set_when_analyze_fn_provided(self, store_with_artifact):
        _, replay_dir = store_with_artifact

        call_count = 0

        def fake_analyze(evidence):
            nonlocal call_count
            call_count += 1
            return {"root_cause": f"hypothesis at step {call_count}", "confidence": 50}

        steps = list(replay_stepwise("INC001", replay_dir, analyze_fn=fake_analyze))
        assert call_count == len(RECEIPTS)
        for i, step in enumerate(steps, start=1):
            assert step.partial_result is not None
            assert step.partial_result["root_cause"] == f"hypothesis at step {i}"

    def test_partial_result_none_when_no_analyze_fn(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        steps = list(replay_stepwise("INC001", replay_dir))
        for step in steps:
            assert step.partial_result is None

    def test_analyze_fn_exception_does_not_stop_replay(self, store_with_artifact):
        _, replay_dir = store_with_artifact

        def bad_analyze(evidence):
            raise RuntimeError("analysis failed")

        # Should yield all steps despite analysis errors
        steps = list(replay_stepwise("INC001", replay_dir, analyze_fn=bad_analyze))
        assert len(steps) == len(RECEIPTS)
        for step in steps:
            assert step.partial_result is None

    def test_analyze_fn_receives_growing_evidence(self, store_with_artifact):
        _, replay_dir = store_with_artifact
        evidence_sizes = []

        def record_sizes(evidence):
            evidence_sizes.append(len(evidence))
            return {}

        list(replay_stepwise("INC001", replay_dir, analyze_fn=record_sizes))
        # Evidence should grow monotonically
        for i in range(1, len(evidence_sizes)):
            assert evidence_sizes[i] >= evidence_sizes[i - 1]


# ---------------------------------------------------------------------------
# ReplayStep dataclass
# ---------------------------------------------------------------------------

class TestReplayStep:
    def test_default_fields(self):
        step = ReplayStep(step_num=1, total_steps=5, receipt={"action": "test"})
        assert step.evidence_snapshot == {}
        assert step.partial_result is None

    def test_stores_all_fields(self):
        step = ReplayStep(
            step_num=2,
            total_steps=5,
            receipt={"action": "search_logs"},
            evidence_snapshot={"search_logs": {"logs": []}},
            partial_result={"root_cause": "test"},
        )
        assert step.step_num == 2
        assert step.total_steps == 5
        assert step.receipt["action"] == "search_logs"
        assert "search_logs" in step.evidence_snapshot
        assert step.partial_result["root_cause"] == "test"
