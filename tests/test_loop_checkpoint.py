"""
Tests for LoopCheckpoint — the loop-operator safety gate.

Verifies:
- No escalation when evidence grows each checkpoint
- Escalation after max_stall_checkpoints consecutive no-progress checkpoints
- Progress resets the stall counter
- should_check fires at the correct call intervals
"""

import pytest

from supervisor.guardrails import ExecutionBudget, LoopCheckpoint, LOOP_CHECKPOINT_INTERVAL


class TestLoopCheckpointShouldCheck:

    def test_not_triggered_at_zero_calls(self):
        budget = ExecutionBudget()
        cp = LoopCheckpoint()
        assert not cp.should_check(budget)

    def test_triggered_at_checkpoint_interval(self):
        budget = ExecutionBudget()
        cp = LoopCheckpoint(checkpoint_interval=4)
        for _ in range(4):
            budget.record_call()
        assert cp.should_check(budget)

    def test_not_triggered_between_intervals(self):
        budget = ExecutionBudget()
        cp = LoopCheckpoint(checkpoint_interval=4)
        for _ in range(3):
            budget.record_call()
        assert not cp.should_check(budget)

    def test_triggered_at_second_interval(self):
        budget = ExecutionBudget()
        cp = LoopCheckpoint(checkpoint_interval=4)
        for _ in range(8):
            budget.record_call()
        assert cp.should_check(budget)

    def test_default_interval_matches_constant(self):
        cp = LoopCheckpoint()
        assert cp.checkpoint_interval == LOOP_CHECKPOINT_INTERVAL


class TestLoopCheckpointProgress:

    def test_no_escalation_when_evidence_grows(self):
        cp = LoopCheckpoint(max_stall_checkpoints=2)
        # Each checkpoint adds new evidence key
        result = cp.check({"logs"}, 0)
        assert result is None
        result = cp.check({"logs", "metrics"}, 0)
        assert result is None
        result = cp.check({"logs", "metrics", "events"}, 0)
        assert result is None

    def test_no_escalation_when_score_improves(self):
        cp = LoopCheckpoint(max_stall_checkpoints=2)
        result = cp.check({"logs"}, 0)
        assert result is None
        result = cp.check({"logs"}, 5)    # score improved by 5
        assert result is None
        result = cp.check({"logs"}, 10)   # score improved again
        assert result is None

    def test_escalation_after_max_stalls(self):
        cp = LoopCheckpoint(max_stall_checkpoints=2)
        # First checkpoint — establishes baseline
        result = cp.check({"logs"}, 0)
        assert result is None
        # Second checkpoint — no new keys, score unchanged → stall 1
        result = cp.check({"logs"}, 0)
        assert result is None
        assert cp.stall_count == 1
        # Third checkpoint — still no progress → stall 2 → escalation
        result = cp.check({"logs"}, 0)
        assert result is not None
        assert result["escalation_trigger"] == "no_progress"
        assert cp.stall_count == 2

    def test_stall_resets_on_progress(self):
        cp = LoopCheckpoint(max_stall_checkpoints=2)
        cp.check({"logs"}, 0)      # baseline
        cp.check({"logs"}, 0)      # stall 1
        assert cp.stall_count == 1
        cp.check({"logs", "metrics"}, 0)  # progress! stall resets
        assert cp.stall_count == 0

    def test_escalation_contains_required_fields(self):
        cp = LoopCheckpoint(max_stall_checkpoints=1)
        cp.check({"logs"}, 0)
        result = cp.check({"logs"}, 0)
        assert result is not None
        assert "escalation_trigger" in result
        assert "checkpoint_number" in result
        assert "stall_count" in result
        assert "evidence_keys" in result
        assert "top_score" in result
        assert "recommendation" in result

    def test_context_keys_excluded_from_progress_check(self):
        """itsm_context, confluence_context etc. should not count as evidence progress."""
        cp = LoopCheckpoint(max_stall_checkpoints=2)
        cp.check({"logs"}, 0)  # baseline with logs
        # Adding only context keys should not count as progress
        result = cp.check({"logs", "itsm_context", "confluence_context"}, 0)
        assert result is None
        assert cp.stall_count == 1  # still a stall

    def test_deterministic_across_runs(self):
        """Same inputs produce the same escalation result."""
        cp1 = LoopCheckpoint(max_stall_checkpoints=1)
        cp1.check({"logs"}, 0)
        r1 = cp1.check({"logs"}, 0)

        cp2 = LoopCheckpoint(max_stall_checkpoints=1)
        cp2.check({"logs"}, 0)
        r2 = cp2.check({"logs"}, 0)

        assert r1 == r2


class TestLoopCheckpointIntegration:

    def test_full_investigation_no_escalation(self):
        """Simulate a healthy investigation that doesn't trigger escalation."""
        cp = LoopCheckpoint(checkpoint_interval=2, max_stall_checkpoints=2)
        budget = ExecutionBudget(max_calls=10)
        evidence = {}

        for i, (new_key, score) in enumerate([
            ("fetch_incident", 0),
            ("search_logs", 20),
            ("golden_signals", 40),
            ("metrics", 55),
            ("changes", 65),
        ]):
            budget.record_call()
            evidence[new_key] = {"data": "..."}
            if cp.should_check(budget):
                result = cp.check(set(evidence.keys()), score)
                assert result is None, f"Unexpected escalation at step {i}"

    def test_stalled_investigation_escalates(self):
        """Simulate an investigation where workers return empty results."""
        cp = LoopCheckpoint(checkpoint_interval=2, max_stall_checkpoints=2)
        budget = ExecutionBudget(max_calls=10)
        # All calls return empty — same evidence keys throughout
        evidence = {"fetch_incident": {}}

        escalated = False
        for _ in range(6):
            budget.record_call()
            if cp.should_check(budget):
                result = cp.check(set(evidence.keys()), 0)
                if result is not None:
                    escalated = True
                    break

        assert escalated, "Investigation should have escalated after repeated stalls"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
