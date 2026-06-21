"""Tests for stagnation detection in AgenticPlanner (PLANNER_STAGNATION_DETECTION)."""
import os
import pytest
from unittest.mock import MagicMock


def _make_planner(steps, max_iterations=10):
    """Build an AgenticPlanner with a mock LLM that returns the given steps in order.

    steps: list of (worker, action) tuples. When exhausted, returns done=True.
    """
    from supervisor.planner import AgenticPlanner, PlannerStep

    step_iter = iter(steps)

    def mock_llm_fn(**kwargs):
        try:
            worker, action = next(step_iter)
            import json
            return {"text": json.dumps({"worker": worker, "action": action, "params": {}, "rationale": "test", "done": False})}
        except StopIteration:
            import json
            return {"text": json.dumps({"worker": "done", "action": "done", "params": {}, "rationale": "done", "done": True})}

    # Mock budget that always has capacity
    budget = MagicMock()
    budget.can_call.return_value = True

    # Mock worker that returns a trivial result
    mock_worker = MagicMock()
    mock_worker.execute.return_value = {"data": "ok"}
    mock_worker._handlers = {"some_action": None}

    workers = {"mock_worker": mock_worker}

    planner = AgenticPlanner(
        workers=workers,
        llm_fn=mock_llm_fn,
        budget=budget,
        max_iterations=max_iterations,
    )
    return planner


class TestStagnationDetection:

    def test_stagnation_disabled_no_detection(self):
        """With PLANNER_STAGNATION_DETECTION=false, planner does not detect stagnation."""
        # 5 identical rounds — should NOT trigger stagnation
        steps = [("mock_worker", "some_action")] * 5
        planner = _make_planner(steps, max_iterations=5)
        incident = {"summary": "test", "affected_service": "svc"}
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PLANNER_STAGNATION_DETECTION", "false")
            evidence, trace = planner.run("INC-1", incident, "error_spike")
        assert trace.stagnation_detected is False

    def test_stagnation_enabled_exits_after_two_duplicate_rounds(self):
        """With PLANNER_STAGNATION_DETECTION=true, after 2 identical rounds, loop exits early."""
        # 5 identical rounds — should trigger after 2 duplicates (rounds 2 and 3 are same as round 1)
        steps = [("mock_worker", "some_action")] * 5
        planner = _make_planner(steps, max_iterations=10)
        incident = {"summary": "test", "affected_service": "svc"}
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PLANNER_STAGNATION_DETECTION", "true")
            evidence, trace = planner.run("INC-1", incident, "error_spike")
        assert trace.stagnation_detected is True
        # Should have exited well before max_iterations=10
        assert trace.iterations < 10

    def test_one_duplicate_round_does_not_trigger(self):
        """After 1 identical round (not yet 2), loop continues."""
        # Round 1: (mock_worker, some_action)
        # Round 2: (mock_worker, some_action)  <- 1 duplicate, counter=1
        # Round 3: (mock_worker, other_action) <- different, resets counter
        steps = [
            ("mock_worker", "some_action"),
            ("mock_worker", "some_action"),
            ("mock_worker", "other_action"),
        ]
        planner = _make_planner(steps, max_iterations=10)
        incident = {"summary": "test", "affected_service": "svc"}
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PLANNER_STAGNATION_DETECTION", "true")
            evidence, trace = planner.run("INC-1", incident, "error_spike")
        # Should NOT have detected stagnation (only 1 dup before reset)
        assert trace.stagnation_detected is False

    def test_duplicate_then_different_resets_counter(self):
        """After identical-then-different round, counter resets to 0."""
        # Round 1: (mock_worker, action_a)
        # Round 2: (mock_worker, action_a)  <- dup_count=1
        # Round 3: (mock_worker, action_b)  <- different -> reset to 0
        # Round 4: (mock_worker, action_a)  <- dup_count=1 again
        # Round 5: done
        steps = [
            ("mock_worker", "action_a"),
            ("mock_worker", "action_a"),
            ("mock_worker", "action_b"),
            ("mock_worker", "action_a"),
        ]
        planner = _make_planner(steps, max_iterations=10)
        incident = {"summary": "test", "affected_service": "svc"}
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PLANNER_STAGNATION_DETECTION", "true")
            evidence, trace = planner.run("INC-1", incident, "error_spike")
        assert trace.stagnation_detected is False

    def test_planner_trace_stagnation_detected_false_for_normal(self):
        """PlannerTrace.stagnation_detected is False for normal completion."""
        from supervisor.planner import PlannerTrace
        trace = PlannerTrace()
        assert trace.stagnation_detected is False

    def test_stagnation_detected_true_in_trace_when_triggered(self):
        """stagnation_detected = True in trace when triggered."""
        steps = [("mock_worker", "same_action")] * 6
        planner = _make_planner(steps, max_iterations=20)
        incident = {"summary": "test", "affected_service": "svc"}
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("PLANNER_STAGNATION_DETECTION", "true")
            evidence, trace = planner.run("INC-2", incident, "error_spike")
        assert trace.stagnation_detected is True
        # Also verify it's in the planner_trace evidence dict
        pt = evidence.get("_planner_trace", {})
        assert pt.get("stagnation_detected") is True
