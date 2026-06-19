"""Tests for supervisor/planner.py — AgenticPlanner Think→Act→Observe loop."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from supervisor.planner import AgenticPlanner, PlannerStep, PlannerTrace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_budget(can_call: bool = True, remaining: int = 10):
    budget = MagicMock()
    budget.can_call.return_value = can_call
    budget.remaining.return_value = remaining
    return budget


def _make_worker(result: dict | None = None):
    worker = MagicMock()
    worker.execute.return_value = result or {"data": "ok"}
    worker._handlers = {"check": None}
    return worker


def _make_llm_fn(step_json: dict | None = None, raises: bool = False):
    """Return a mock llm_fn.

    If raises=True, calling it raises RuntimeError.
    If step_json is None, returns an error response.
    """
    def _fn(**kwargs):
        if raises:
            raise RuntimeError("LLM unavailable")
        if step_json is None:
            return {"error": "disabled", "text": ""}
        return {"text": json.dumps(step_json), "error": None}

    return _fn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_planner_done_on_first_step():
    """LLM returns done=true → loop exits after 1 iteration, evidence returned."""
    step = {"worker": "metrics_worker", "action": "check", "params": {}, "rationale": "r", "done": True}
    planner = AgenticPlanner(
        workers={"metrics_worker": _make_worker()},
        llm_fn=_make_llm_fn(step),
        budget=_make_budget(),
        max_iterations=5,
    )
    evidence, trace = planner.run("inc-1", {"summary": "test"}, "error_spike")

    assert trace.iterations == 1
    assert "_planner_trace" in evidence
    assert evidence["_planner_trace"]["iterations"] == 1
    # done=True means no Act was executed
    assert len(trace.steps) == 0


def test_planner_executes_tool():
    """LLM returns valid step → worker.execute() called with correct args."""
    step = {"worker": "metrics_worker", "action": "check", "params": {"k": "v"}, "rationale": "r", "done": False}
    worker = _make_worker({"cpu": 99})

    # LLM returns the step on first call, done=true on second
    done_step = {**step, "done": True}
    call_count = [0]

    def _llm_fn(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"text": json.dumps(step), "error": None}
        return {"text": json.dumps(done_step), "error": None}

    planner = AgenticPlanner(
        workers={"metrics_worker": worker},
        llm_fn=_llm_fn,
        budget=_make_budget(),
        max_iterations=5,
    )
    evidence, trace = planner.run("inc-2", {"summary": "cpu high"}, "resource_exhaustion")

    worker.execute.assert_called_once_with("check", {"k": "v"})
    assert "metrics_worker_check" in evidence
    assert len(trace.steps) == 1


def test_planner_falls_back_when_llm_unavailable():
    """llm_fn raises → fallback_step used, fallback_used=True in trace."""
    fallback_playbook = [
        {"worker": "log_worker", "action": "fetch", "label": "fetch_logs"},
    ]
    worker = _make_worker({"logs": ["line1"]})

    # LLM raises on every call — loop will try fallback each time
    # We limit iterations to 1 to keep test tight
    planner = AgenticPlanner(
        workers={"log_worker": worker},
        llm_fn=_make_llm_fn(raises=True),
        budget=_make_budget(),
        fallback_playbook=fallback_playbook,
        max_iterations=1,
    )
    evidence, trace = planner.run("inc-3", {"summary": "logs missing"}, "error_spike")

    assert trace.fallback_used is True
    assert evidence["_planner_trace"]["fallback_used"] is True
    # Fallback step was executed
    worker.execute.assert_called_once()


def test_planner_respects_max_iterations():
    """LLM always returns done=false → exits at max_iterations."""
    step = {"worker": "metrics_worker", "action": "check", "params": {}, "rationale": "r", "done": False}
    worker = _make_worker({"x": 1})

    planner = AgenticPlanner(
        workers={"metrics_worker": worker},
        llm_fn=_make_llm_fn(step),
        budget=_make_budget(),
        max_iterations=3,
    )
    evidence, trace = planner.run("inc-4", {"summary": "loop"}, "error_spike")

    assert trace.iterations == 3
    assert len(trace.steps) == 3


def test_planner_respects_budget():
    """budget.can_call() returns False → exits immediately."""
    step = {"worker": "metrics_worker", "action": "check", "params": {}, "rationale": "r", "done": False}
    worker = _make_worker()

    planner = AgenticPlanner(
        workers={"metrics_worker": worker},
        llm_fn=_make_llm_fn(step),
        budget=_make_budget(can_call=False),
        max_iterations=5,
    )
    evidence, trace = planner.run("inc-5", {"summary": "budget test"}, "error_spike")

    # Budget was exhausted before any iteration completed an act
    worker.execute.assert_not_called()
    assert trace.iterations == 1  # loop ran once, checked budget, broke


def test_parse_response_valid_json():
    """Valid JSON text → PlannerStep with correct fields."""
    planner = AgenticPlanner(workers={}, llm_fn=lambda **k: {}, budget=_make_budget())
    text = '{"worker": "log_worker", "action": "fetch", "params": {"limit": 100}, "rationale": "check logs", "done": false}'
    step = planner._parse_response(text)

    assert step is not None
    assert step.worker == "log_worker"
    assert step.action == "fetch"
    assert step.params == {"limit": 100}
    assert step.rationale == "check logs"
    assert step.done is False


def test_parse_response_with_markdown_fences():
    """```json ... ``` wrapped → parsed correctly."""
    planner = AgenticPlanner(workers={}, llm_fn=lambda **k: {}, budget=_make_budget())
    text = '```json\n{"worker": "metrics_worker", "action": "check", "params": {}, "rationale": "r", "done": true}\n```'
    step = planner._parse_response(text)

    assert step is not None
    assert step.worker == "metrics_worker"
    assert step.done is True


def test_parse_response_invalid_json():
    """Garbage text → returns None."""
    planner = AgenticPlanner(workers={}, llm_fn=lambda **k: {}, budget=_make_budget())
    step = planner._parse_response("not json at all !!!")
    assert step is None


def test_observe_merges_result_into_evidence():
    """result dict merged into evidence under label key and top-level."""
    planner = AgenticPlanner(workers={}, llm_fn=lambda **k: {}, budget=_make_budget())
    step = PlannerStep(worker="metrics_worker", action="check")
    result = {"cpu": 80, "memory": 60}
    evidence = {"existing_key": "val"}

    updated = planner._observe(step, result, evidence)

    assert "metrics_worker_check" in updated
    assert updated["cpu"] == 80
    assert updated["memory"] == 60
    assert updated["existing_key"] == "val"


def test_planner_trace_records_steps():
    """After 2 iterations, trace.steps has 2 entries."""
    steps_sequence = [
        {"worker": "metrics_worker", "action": "check", "params": {}, "rationale": "r1", "done": False},
        {"worker": "log_worker", "action": "fetch", "params": {}, "rationale": "r2", "done": False},
        {"worker": "metrics_worker", "action": "check", "params": {}, "rationale": "r3", "done": True},
    ]
    call_count = [0]

    def _llm_fn(**kwargs):
        idx = min(call_count[0], len(steps_sequence) - 1)
        call_count[0] += 1
        return {"text": json.dumps(steps_sequence[idx]), "error": None}

    workers = {
        "metrics_worker": _make_worker({"cpu": 50}),
        "log_worker": _make_worker({"logs": []}),
    }

    planner = AgenticPlanner(
        workers=workers,
        llm_fn=_llm_fn,
        budget=_make_budget(),
        max_iterations=5,
    )
    _, trace = planner.run("inc-6", {"summary": "trace test"}, "error_spike")

    assert len(trace.steps) == 2
    assert trace.steps[0]["worker"] == "metrics_worker"
    assert trace.steps[1]["worker"] == "log_worker"
