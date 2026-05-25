"""Execution guardrails for SentinalAI.

Provides:
- Hard timeouts for worker calls
- Max call budgets per investigation phase
- Circuit breaker for flaky workers
- Policy-based query validation
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from supervisor.eval_metrics import record_circuit_breaker_trip

logger = logging.getLogger(__name__)

# =========================================================================
# Execution limits
# =========================================================================

MAX_TOOL_CALLS_PER_CASE = int(
    os.environ.get("INVESTIGATION_BUDGET_MAX_CALLS", "20")
)
MAX_RETRIES_PER_CALL = 2
CALL_TIMEOUT_SECONDS = float(
    os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "30")
)
MAX_CONCURRENT_WORKERS = 5

PHASE_CALL_LIMITS: dict[str, int] = {
    "initial_context": 2,
    "itsm_enrichment": 3,
    "evidence_gathering": 8,
    "change_correlation": 3,
    "devops_enrichment": 2,
    "historical_context": 2,
}


@dataclass
class ExecutionBudget:
    """Tracks and enforces call budget for an investigation.

    Thread-safe: parallel worker groups share a single budget instance and
    race on can_call()/record_call(). Use try_record() for the atomic
    check-and-decrement pattern required in concurrent contexts.
    """

    case_id: str = ""
    max_calls: int = MAX_TOOL_CALLS_PER_CASE
    calls_made: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def can_call(self) -> bool:
        with self._lock:
            return self.calls_made < self.max_calls

    def record_call(self) -> None:
        with self._lock:
            self.calls_made += 1

    def try_record(self) -> bool:
        """Atomically check budget and record a call if available.

        Returns True if the call was recorded (budget had capacity).
        Returns False if the budget was already exhausted.
        Use this instead of separate can_call()/record_call() in concurrent code.
        """
        with self._lock:
            if self.calls_made < self.max_calls:
                self.calls_made += 1
                return True
            return False

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_calls - self.calls_made)


# =========================================================================
# Circuit breaker (per-worker)
# =========================================================================

@dataclass
class CircuitState:
    """Simple circuit breaker for a single worker.

    All state mutations and the is_open check are protected by an internal
    lock so concurrent worker threads cannot race on failure_count or
    last_failure_time.
    """

    failure_count: int = 0
    last_failure_time: float = 0.0
    threshold: int = 3
    recovery_seconds: float = 60.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def is_open(self) -> bool:
        """True if circuit is open (worker should be skipped)."""
        with self._lock:
            return self._is_open_locked()

    def _is_open_locked(self) -> bool:
        """Caller must hold self._lock."""
        if self.failure_count < self.threshold:
            return False
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed > self.recovery_seconds:
            # Allow a probe
            return False
        return True

    def record_failure(self, worker_name: str = "") -> None:
        with self._lock:
            was_open = self._is_open_locked()
            self.failure_count += 1
            self.last_failure_time = time.monotonic()
            if not was_open and self._is_open_locked() and worker_name:
                record_circuit_breaker_trip(worker_name, "closed_to_open")

    def record_success(self, worker_name: str = "") -> None:
        with self._lock:
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
    # Stage 2 domain terms (certificate, identity, credential, messaging, DB)
    "certificate",
    "tls",
    "ssl",
    "kafka",
    "restart",
    "crashloop",
    "credential",
    "ora-",
    "vault",
    "saml",
    "oauth",
    "ldap",
]

# Max Splunk time window (hours)
MAX_SPLUNK_TIME_WINDOW_HOURS = 24

# Max rows per query result
MAX_RESULT_ROWS = 10_000


def validate_query(query: str) -> tuple[bool, str]:
    """Validate a Splunk query against the policy.

    Returns (is_valid, reason).

    Dangerous write/exfiltration commands are blocked by matching the command
    token after a pipe (SPL syntax: ``| <command>``).  The pipe character itself
    is NOT blocked — it is required for all SPL transformation commands and
    blocking it would prevent any analytical query from running.

    ``eval`` is only blocked when used as a pipe command (``| eval``) to prevent
    computed-field injection; the substring "eval" alone would incorrectly block
    legitimate terms like "evaluate", "interval", "medieval".
    """
    if not query or not query.strip():
        return False, "empty query"

    query_lower = query.lower().strip()

    # Block dangerous SPL commands that write data or enumerate sensitive tables.
    # Match as pipe-commands (``| cmd``) or bare command names at string start.
    # This avoids false-positives from innocent substrings (e.g. "eval" in "evaluate").
    dangerous_pipe_commands = [
        "outputlookup", "delete", "collect",
        "| eval", "|eval",
        "| lookup", "|lookup",
    ]
    for cmd in dangerous_pipe_commands:
        if cmd in query_lower:
            return False, f"blocked command: {cmd.strip('| ')}"

    # Validate against allowlist: at least one allowed term must appear
    if not any(term in query_lower for term in SPLUNK_QUERY_ALLOWLIST):
        return False, "query does not match any allowed pattern"

    return True, "ok"


# =========================================================================
# Loop-operator: checkpoint-based progress monitoring
# =========================================================================

# How many worker calls between progress checkpoints
LOOP_CHECKPOINT_INTERVAL = int(os.environ.get("LOOP_CHECKPOINT_INTERVAL", "4"))

# How many consecutive no-progress checkpoints before escalation
LOOP_MAX_STALL_CHECKPOINTS = int(os.environ.get("LOOP_MAX_STALL_CHECKPOINTS", "2"))


@dataclass
class LoopCheckpoint:
    """Tracks evidence progress across investigation checkpoints.

    A checkpoint passes when evidence grows or a hypothesis improves.
    Two consecutive failed checkpoints trigger an escalation.

    Usage::

        cp = LoopCheckpoint()
        # ... call workers, add evidence ...
        if cp.should_check(budget):
            escalation = cp.check(evidence_keys, top_score)
            if escalation:
                # surface escalation to user / emit OTEL
    """

    checkpoint_interval: int = LOOP_CHECKPOINT_INTERVAL
    max_stall_checkpoints: int = LOOP_MAX_STALL_CHECKPOINTS

    _last_evidence_keys: frozenset = field(default_factory=frozenset)
    _last_top_score: int = 0
    _stall_count: int = 0
    _checkpoint_number: int = 0

    def should_check(self, budget: "ExecutionBudget") -> bool:
        """Return True if it's time for a checkpoint (every N calls)."""
        return (budget.calls_made > 0 and
                budget.calls_made % self.checkpoint_interval == 0)

    def check(
        self,
        evidence_keys: set[str],
        top_score: int,
    ) -> dict | None:
        """Evaluate progress since the last checkpoint.

        Returns an escalation dict if stall limit exceeded, otherwise None.
        """
        self._checkpoint_number += 1
        current_keys = frozenset(k for k in evidence_keys if k not in
                                  ("itsm_context", "confluence_context",
                                   "historical_context", "devops_context"))
        new_keys = current_keys - self._last_evidence_keys
        score_improved = top_score >= self._last_top_score + 5

        made_progress = bool(new_keys) or score_improved

        self._last_evidence_keys = current_keys
        self._last_top_score = top_score

        if made_progress:
            self._stall_count = 0
            return None

        self._stall_count += 1
        logger.debug(
            "Loop checkpoint #%d: no progress (stall=%d/%d)",
            self._checkpoint_number, self._stall_count, self.max_stall_checkpoints,
        )

        if self._stall_count >= self.max_stall_checkpoints:
            return {
                "escalation_trigger": "no_progress",
                "checkpoint_number": self._checkpoint_number,
                "stall_count": self._stall_count,
                "evidence_keys": sorted(current_keys),
                "top_score": top_score,
                "recommendation": "Return partial result with LOW CONFIDENCE prefix.",
            }

        return None

    @property
    def stall_count(self) -> int:
        return self._stall_count


