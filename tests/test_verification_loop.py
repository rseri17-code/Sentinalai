"""Tests for the VerificationLoop."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call

from supervisor.verification_loop import VerificationLoop, VerificationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

STABLE_METRICS = {
    "metrics": {
        "error_rate": 0.005,        # 0.5% — well below 1% threshold
        "latency_p95": 95.0,        # ms
    }
}

UNSTABLE_METRICS = {
    "metrics": {
        "error_rate": 0.08,         # 8% — above threshold
        "latency_p95": 800.0,
    }
}

LOG_NO_ERRORS = {"logs": []}
LOG_WITH_ERRORS = {"logs": [{"level": "ERROR", "message": "NullPointerException"}] * 3}

# Baseline set higher than STABLE_METRICS so stable readings pass the check
# (STABLE has error_rate=0.005; baseline*1.1 threshold must be > 0.005)
BASELINE = {"error_rate": 0.01, "latency_p95_ms": 200.0}


def _make_loop(
    metrics_responses=None,
    log_responses=None,
    poll_interval=0,      # 0s for test speed
    max_polls=5,
    stable_threshold=3,
):
    """Build a VerificationLoop with mocked workers."""
    metrics_worker = MagicMock()
    log_worker = MagicMock()

    # Default: always stable
    if metrics_responses is None:
        metrics_worker.execute.return_value = STABLE_METRICS
    else:
        metrics_worker.execute.side_effect = metrics_responses

    if log_responses is None:
        log_worker.execute.return_value = LOG_NO_ERRORS
    else:
        log_worker.execute.side_effect = log_responses

    loop = VerificationLoop(
        metrics_worker=metrics_worker,
        log_worker=log_worker,
        poll_interval_sec=poll_interval,
        max_polls=max_polls,
        stable_threshold=stable_threshold,
    )
    return loop, metrics_worker, log_worker


# ---------------------------------------------------------------------------
# _check_stability
# ---------------------------------------------------------------------------

class TestCheckStability:
    def test_stable_when_error_rate_low(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.005, "latency_p95_ms": 100.0},
            {},
            "",
            "svc",
        ) is True

    def test_unstable_when_error_rate_exceeds_baseline(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.05},
            {"error_rate": 0.004},
            "",
            "svc",
        ) is False

    def test_unstable_when_latency_exceeds_baseline(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.001, "latency_p95_ms": 1000.0},
            {"latency_p95_ms": 100.0},
            "",
            "svc",
        ) is False

    def test_stable_within_10_percent_of_baseline_error(self):
        loop, _, _ = _make_loop()
        # baseline=0.01, current=0.0109 (<= 0.01 * 1.1)
        assert loop._check_stability(
            {"error_rate": 0.0109},
            {"error_rate": 0.01},
            "",
            "svc",
        ) is True

    def test_unstable_when_error_matches_found(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.001, "latency_p95_ms": 50.0, "recent_error_matches": 5},
            {},
            "NullPointerException",
            "svc",
        ) is False

    def test_stable_when_no_error_signature(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.001, "latency_p95_ms": 50.0, "recent_error_matches": 0},
            {},
            "",
            "svc",
        ) is True

    def test_stable_without_baseline_when_error_rate_below_1pct(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.005},
            {},
            "",
            "svc",
        ) is True

    def test_unstable_without_baseline_when_error_rate_above_1pct(self):
        loop, _, _ = _make_loop()
        assert loop._check_stability(
            {"error_rate": 0.05},
            {},
            "",
            "svc",
        ) is False


# ---------------------------------------------------------------------------
# watch — happy path (stable immediately)
# ---------------------------------------------------------------------------

class TestVerificationLoopWatch:
    def test_success_after_stable_threshold(self):
        loop, _, _ = _make_loop(
            max_polls=5,
            stable_threshold=3,
        )
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-001", "payment-service", baseline=BASELINE)
        )
        assert result.success is True
        assert result.stable_readings >= 3

    def test_result_contains_required_fields(self):
        loop, _, _ = _make_loop(max_polls=5, stable_threshold=2)
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-001", "svc")
        )
        assert result.investigation_id == "inv-001"
        assert result.service == "svc"
        assert isinstance(result.total_polls, int)
        assert isinstance(result.duration_sec, float)

    def test_success_result_to_dict(self):
        loop, _, _ = _make_loop(max_polls=3, stable_threshold=2)
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-002", "web-service")
        )
        d = result.to_dict()
        assert d["success"] is True
        assert "stable_readings" in d
        assert "total_polls" in d

    def test_failure_after_max_polls_unstable(self):
        """All polls return unstable metrics → failure."""
        loop, _, _ = _make_loop(
            metrics_responses=[UNSTABLE_METRICS] * 10,
            max_polls=3,
            stable_threshold=3,
        )
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-003", "broken-service")
        )
        assert result.success is False
        assert result.total_polls == 3
        assert "stabilize" in result.failure_reason.lower()

    def test_resets_stable_count_on_unstable_reading(self):
        """stable, stable, unstable, stable, stable, stable → should eventually succeed."""
        responses = [
            STABLE_METRICS,
            STABLE_METRICS,
            UNSTABLE_METRICS,  # resets stable count
            STABLE_METRICS,
            STABLE_METRICS,
            STABLE_METRICS,
        ]
        loop, _, _ = _make_loop(
            metrics_responses=responses,
            max_polls=8,
            stable_threshold=3,
        )
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-004", "flappy-service")
        )
        assert result.success is True

    def test_callback_called_on_success(self):
        """Verify the callback is invoked for verification events."""
        events_received = []

        async def on_event(investigation_id, event_type, data):
            events_received.append(event_type)

        loop, _, _ = _make_loop(max_polls=3, stable_threshold=2)
        asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-005", "svc", callback=on_event)
        )

        assert "verification.started" in events_received
        assert "verification.success" in events_received

    def test_callback_called_on_failure(self):
        events_received = []

        async def on_event(investigation_id, event_type, data):
            events_received.append(event_type)

        loop, _, _ = _make_loop(
            metrics_responses=[UNSTABLE_METRICS] * 5,
            max_polls=3,
            stable_threshold=3,
        )
        asyncio.get_event_loop().run_until_complete(
            loop.watch("inv-006", "svc", callback=on_event)
        )
        assert "verification.failed" in events_received


# ---------------------------------------------------------------------------
# watch — SNOW ticket auto-close
# ---------------------------------------------------------------------------

class TestSnowTicketClose:
    def test_calls_update_incident_on_success(self):
        itsm_worker = MagicMock()
        itsm_worker.execute.return_value = {"updated": {"state": "resolved"}}

        loop, _, _ = _make_loop(max_polls=4, stable_threshold=2)
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch(
                "inv-007",
                "payment-service",
                itsm_worker=itsm_worker,
                incident_id="INC0099999",
            )
        )
        assert result.success is True
        itsm_worker.execute.assert_called()
        call_action = itsm_worker.execute.call_args[0][0]
        assert call_action == "update_incident"

    def test_does_not_call_update_incident_on_failure(self):
        itsm_worker = MagicMock()

        loop, _, _ = _make_loop(
            metrics_responses=[UNSTABLE_METRICS] * 5,
            max_polls=3,
            stable_threshold=3,
        )
        asyncio.get_event_loop().run_until_complete(
            loop.watch(
                "inv-008",
                "broken-service",
                itsm_worker=itsm_worker,
                incident_id="INC0099998",
            )
        )
        itsm_worker.execute.assert_not_called()

    def test_ticket_closed_flag_set_on_success(self):
        itsm_worker = MagicMock()
        itsm_worker.execute.return_value = {"updated": {"state": "resolved"}}

        loop, _, _ = _make_loop(max_polls=3, stable_threshold=2)
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch(
                "inv-009",
                "svc",
                itsm_worker=itsm_worker,
                incident_id="INC0001",
            )
        )
        assert result.closed_ticket is True

    def test_graceful_degradation_when_itsm_fails(self):
        """ITSM failure should not break the verification result."""
        itsm_worker = MagicMock()
        itsm_worker.execute.side_effect = RuntimeError("SNOW unreachable")

        loop, _, _ = _make_loop(max_polls=3, stable_threshold=2)
        result = asyncio.get_event_loop().run_until_complete(
            loop.watch(
                "inv-010",
                "svc",
                itsm_worker=itsm_worker,
                incident_id="INC0001",
            )
        )
        # Loop itself should still succeed
        assert result.success is True
        # But ticket wasn't closed
        assert result.closed_ticket is False
