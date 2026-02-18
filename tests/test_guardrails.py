"""Tests for execution guardrails."""

import time

import pytest

from supervisor.guardrails import (
    ExecutionBudget,
    CircuitState,
    CircuitBreakerRegistry,
    validate_query,
    MAX_TOOL_CALLS_PER_CASE,
)


class TestExecutionBudget:
    def test_initial_state(self):
        b = ExecutionBudget(case_id="INC1")
        assert b.can_call()
        assert b.remaining() == MAX_TOOL_CALLS_PER_CASE

    def test_budget_exhaustion(self):
        b = ExecutionBudget(case_id="INC1", max_calls=3)
        for _ in range(3):
            assert b.can_call()
            b.record_call()
        assert not b.can_call()
        assert b.remaining() == 0

    def test_remaining_decreases(self):
        b = ExecutionBudget(case_id="INC1", max_calls=5)
        b.record_call()
        assert b.remaining() == 4


class TestCircuitState:
    def test_starts_closed(self):
        cs = CircuitState()
        assert not cs.is_open

    def test_opens_after_threshold(self):
        cs = CircuitState(threshold=2, recovery_seconds=60)
        cs.record_failure()
        assert not cs.is_open
        cs.record_failure()
        assert cs.is_open

    def test_success_resets(self):
        cs = CircuitState(threshold=2)
        cs.record_failure()
        cs.record_failure()
        assert cs.is_open
        cs.record_success()
        assert not cs.is_open

    def test_recovery_allows_probe(self):
        cs = CircuitState(threshold=1, recovery_seconds=0.01)
        cs.record_failure()
        assert cs.is_open
        time.sleep(0.02)
        # After recovery period, should allow a probe
        assert not cs.is_open


class TestCircuitBreakerRegistry:
    def test_returns_same_circuit_for_same_worker(self):
        reg = CircuitBreakerRegistry()
        c1 = reg.get("worker_a")
        c2 = reg.get("worker_a")
        assert c1 is c2

    def test_different_workers_get_different_circuits(self):
        reg = CircuitBreakerRegistry()
        c1 = reg.get("worker_a")
        c2 = reg.get("worker_b")
        assert c1 is not c2


class TestValidateQuery:
    def test_valid_query(self):
        ok, reason = validate_query("timeout errors in payment-service")
        assert ok
        assert reason == "ok"

    def test_empty_query(self):
        ok, reason = validate_query("")
        assert not ok

    def test_pipe_blocked(self):
        ok, reason = validate_query("search | eval foo=bar")
        assert not ok
        assert "blocked pattern" in reason

    def test_eval_blocked(self):
        ok, reason = validate_query("search eval something")
        assert not ok
        assert "blocked pattern" in reason

    def test_delete_blocked(self):
        ok, reason = validate_query("delete all records")
        assert not ok
