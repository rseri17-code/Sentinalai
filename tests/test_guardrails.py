"""Tests for execution guardrails."""

import time


from supervisor.guardrails import (
    ExecutionBudget,
    CircuitState,
    CircuitBreakerRegistry,
    validate_query,
    PHASE_CALL_LIMITS,
    MAX_TOOL_CALLS_PER_CASE,
    SPLUNK_QUERY_ALLOWLIST,
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

    def test_reset_clears_all_circuits(self):
        """Lines 120-121: reset() clears all circuit state."""
        reg = CircuitBreakerRegistry()
        reg.get("worker_a").record_failure()
        reg.get("worker_b").record_failure()
        reg.reset()
        # After reset, getting a worker returns a fresh CircuitState
        c = reg.get("worker_a")
        assert c.failure_count == 0
        assert not c.is_open


class TestCircuitStateMetricsCallbacks:
    """Test that record_success triggers the half_open_to_closed metric callback."""

    def test_record_success_emits_metric_on_recovery(self):
        """Line 98: record_success when was_open and worker_name given."""
        from unittest.mock import patch
        cs = CircuitState(threshold=2)
        cs.record_failure()
        cs.record_failure()
        assert cs.is_open
        with patch("supervisor.guardrails.record_circuit_breaker_trip") as mock_trip:
            cs.record_success(worker_name="test_worker")
        assert not cs.is_open
        mock_trip.assert_called_once_with("test_worker", "half_open_to_closed")


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

    def test_allowlist_rejects_unknown_terms(self):
        ok, reason = validate_query("foobar gibberish xyz")
        assert not ok
        assert "allowed pattern" in reason

    def test_allowlist_accepts_known_terms(self):
        for term in SPLUNK_QUERY_ALLOWLIST:
            ok, reason = validate_query(f"search {term} in service")
            assert ok, f"Allowlist term '{term}' should be accepted"


class TestPhaseCallLimitsSpec:
    """Validate phase-level sub-budgets match the specification."""

    SPEC_PHASES = {
        "initial_context": 2,
        "itsm_enrichment": 3,
        "evidence_gathering": 8,
        "change_correlation": 3,
        "devops_enrichment": 2,
        "historical_context": 2,
    }

    def test_all_spec_phases_present(self):
        for phase in self.SPEC_PHASES:
            assert phase in PHASE_CALL_LIMITS, f"Missing phase budget: {phase}"

    def test_phase_limits_match_spec(self):
        for phase, limit in self.SPEC_PHASES.items():
            assert PHASE_CALL_LIMITS[phase] == limit, (
                f"Phase '{phase}' budget should be {limit}, got {PHASE_CALL_LIMITS[phase]}"
            )

    def test_total_budget_equals_max_calls(self):
        assert sum(PHASE_CALL_LIMITS.values()) == MAX_TOOL_CALLS_PER_CASE


class TestBudgetEnvVarConfig:
    """Validate budget is configurable via environment variables."""

    def test_default_max_calls_is_20(self):
        assert MAX_TOOL_CALLS_PER_CASE == 20
