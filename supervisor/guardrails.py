"""Execution guardrails for SentinalAI.

Provides:
- Hard timeouts for worker calls
- Max call budgets per investigation phase
- Circuit breaker for flaky workers
- Policy-based query validation
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from supervisor.eval_metrics import record_circuit_breaker_trip, record_budget_exhausted

logger = logging.getLogger(__name__)

# =========================================================================
# Execution limits
# =========================================================================

MAX_TOOL_CALLS_PER_CASE = 20
MAX_RETRIES_PER_CALL = 2
CALL_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_WORKERS = 5

PHASE_CALL_LIMITS: dict[str, int] = {
    "initial_context": 2,
    "evidence_gathering": 8,
    "change_correlation": 3,
    "historical_context": 2,
}


@dataclass
class ExecutionBudget:
    """Tracks and enforces call budget for an investigation."""

    case_id: str = ""
    max_calls: int = MAX_TOOL_CALLS_PER_CASE
    calls_made: int = 0

    def can_call(self) -> bool:
        return self.calls_made < self.max_calls

    def record_call(self) -> None:
        self.calls_made += 1

    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls_made)


# =========================================================================
# Circuit breaker (per-worker)
# =========================================================================

@dataclass
class CircuitState:
    """Simple circuit breaker for a single worker."""

    failure_count: int = 0
    last_failure_time: float = 0.0
    threshold: int = 3
    recovery_seconds: float = 60.0

    @property
    def is_open(self) -> bool:
        """True if circuit is open (worker should be skipped)."""
        if self.failure_count < self.threshold:
            return False
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed > self.recovery_seconds:
            # Allow a probe
            return False
        return True

    def record_failure(self, worker_name: str = "") -> None:
        was_open = self.is_open
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if not was_open and self.is_open and worker_name:
            record_circuit_breaker_trip(worker_name, "closed_to_open")

    def record_success(self, worker_name: str = "") -> None:
        was_open = self.failure_count >= self.threshold
        self.failure_count = 0
        if was_open and worker_name:
            record_circuit_breaker_trip(worker_name, "half_open_to_closed")


class CircuitBreakerRegistry:
    """Registry of circuit breakers, keyed by worker name.

    Can be instantiated per-investigation for isolation, or shared
    globally for cross-investigation state (e.g., real flaky backends).
    """

    def __init__(self):
        self._circuits: dict[str, CircuitState] = {}
        self._lock = threading.Lock()

    def get(self, worker_name: str) -> CircuitState:
        with self._lock:
            if worker_name not in self._circuits:
                self._circuits[worker_name] = CircuitState()
            return self._circuits[worker_name]

    def reset(self) -> None:
        """Clear all circuit state. Used between investigations for isolation."""
        with self._lock:
            self._circuits.clear()


# Global registry — kept for backward compatibility but prefer per-investigation.
circuit_registry = CircuitBreakerRegistry()


# =========================================================================
# Policy: query validation
# =========================================================================

# Allowed Splunk query patterns (prefix allowlist)
SPLUNK_QUERY_ALLOWLIST = [
    "timeout",
    "oomkill",
    "oom",
    "error",
    "latency",
    "slow",
    "cpu",
    "memory",
    "heap",
    "thread",
    "dns",
    "connection",
    "network",
    "cascade",
    "pipeline",
    "auth",
    "notification",
    "recommendation",
]

# Max Splunk time window (hours)
MAX_SPLUNK_TIME_WINDOW_HOURS = 24

# Max rows per query result
MAX_RESULT_ROWS = 10_000


def validate_query(query: str) -> tuple[bool, str]:
    """Validate a Splunk query against the policy.

    Returns (is_valid, reason).
    """
    if not query or not query.strip():
        return False, "empty query"

    query_lower = query.lower().strip()

    # Block shell injection / dangerous patterns
    dangerous = ["|", "eval", "lookup", "outputlookup", "delete", "collect"]
    for d in dangerous:
        if d in query_lower:
            return False, f"blocked pattern: {d}"

    return True, "ok"
