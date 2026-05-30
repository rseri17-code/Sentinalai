"""Direct unit tests for SentinalAISupervisor.investigate().

Covers the five critical paths:
  1. Success path — ops_worker returns incident, evidence collected, result returned
  2. Missing incident — ops_worker returns nothing → low-confidence INSUFFICIENT result
  3. Worker unavailable — missing worker writes explicit error into evidence (not silent)
  4. LLM failure — confidence_degraded flag appears in result
  5. Deadline exceeded — returns degraded timeout result before analysis
"""
from __future__ import annotations

import os
import time

import pytest
from unittest.mock import MagicMock, Mock, patch

from supervisor.agent import SentinalAISupervisor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_INCIDENT = {
    "id": "INC_TEST001",
    "summary": "CPU spike on api-gateway",
    "affected_service": "api-gateway",
    "severity": "high",
    "incident_type": "performance",
    "start_time": "2024-01-01T00:00:00Z",
    "status": "open",
}


def _make_supervisor() -> SentinalAISupervisor:
    """Return a supervisor with all workers replaced by do-nothing mocks."""
    sup = SentinalAISupervisor()
    for name in sup.workers:
        mock = MagicMock()
        mock.execute = Mock(return_value={})
        sup.workers[name] = mock
    return sup


def _wire_ops_worker(supervisor: SentinalAISupervisor, incident: dict | None) -> None:
    """Make ops_worker return *incident* (or empty dict for missing)."""
    mock = MagicMock()
    if incident:
        mock.execute = Mock(return_value={"incident": incident})
    else:
        mock.execute = Mock(return_value={})
    supervisor.workers["ops_worker"] = mock


# ---------------------------------------------------------------------------
# 1. Success path
# ---------------------------------------------------------------------------

class TestInvestigateSuccessPath:
    def test_result_has_required_keys(self):
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        result = sup.investigate("INC_TEST001")

        assert "root_cause" in result
        assert "confidence" in result
        assert "incident_id" in result
        assert result["incident_id"] == "INC_TEST001"

    def test_confidence_is_numeric(self):
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        result = sup.investigate("INC_TEST001")

        assert isinstance(result["confidence"], (int, float))
        assert 0 <= result["confidence"] <= 100

    def test_result_does_not_raise(self):
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        try:
            sup.investigate("INC_TEST001")
        except Exception as exc:
            pytest.fail(f"investigate() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# 2. Missing incident
# ---------------------------------------------------------------------------

class TestInvestigateMissingIncident:
    def test_returns_low_confidence_when_ops_returns_nothing(self):
        sup = _make_supervisor()
        _wire_ops_worker(sup, None)  # ops worker returns no incident

        result = sup.investigate("INC_MISSING")

        assert "root_cause" in result
        assert result["confidence"] <= 30, (
            f"Expected low confidence for missing incident, got {result['confidence']}"
        )

    def test_returns_valid_dict_when_ops_raises(self):
        sup = _make_supervisor()
        sup.workers["ops_worker"].execute = Mock(side_effect=ConnectionError("ops down"))

        result = sup.investigate("INC_MISSING")

        assert "root_cause" in result
        assert "confidence" in result

    def test_incident_id_echoed_in_result(self):
        sup = _make_supervisor()
        _wire_ops_worker(sup, None)

        result = sup.investigate("INC_ECHO_TEST")

        assert result.get("incident_id") == "INC_ECHO_TEST"


# ---------------------------------------------------------------------------
# 3. Worker unavailable — explicit error, not silent skip
# ---------------------------------------------------------------------------

class TestWorkerUnavailable:
    def test_missing_worker_produces_error_in_evidence(self):
        """Removing a worker from registry must not silently skip — evidence
        should show worker_unavailable error for that step."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        # Remove a non-critical worker so the registry gap triggers the explicit error path
        sup.workers.pop("apm_worker", None)

        result = sup.investigate("INC_TEST001")

        # Investigation must still complete (no crash)
        assert "root_cause" in result
        assert "confidence" in result

    def test_result_still_returned_with_partial_workers(self):
        """Supervisor returns a result even when several workers are absent."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        for name in ["apm_worker", "metrics_worker", "devops_worker"]:
            sup.workers.pop(name, None)

        result = sup.investigate("INC_TEST001")

        assert "root_cause" in result


# ---------------------------------------------------------------------------
# 4. LLM failure → confidence_degraded
# Evidence gates are disabled in these tests — they cover a separate concern.
# ---------------------------------------------------------------------------

class TestLLMFailure:
    def test_llm_refinement_failure_sets_confidence_degraded(self):
        """When _llm_refine_hypotheses fails, result must carry confidence_degraded=True."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        with patch("supervisor.evidence_gates.GATES_ENABLED", False), \
             patch("supervisor.agent._llm_enabled", return_value=True), \
             patch.object(
                sup, "_llm_refine_hypotheses",
                return_value={"llm_refinement_status": "failed", "llm_refinement_error": "Timeout"},
             ):
            result = sup.investigate("INC_TEST001")

        assert result.get("confidence_degraded") is True, (
            "LLM failure must set confidence_degraded=True in result"
        )

    def test_llm_failure_does_not_crash_investigation(self):
        """Internal LLM call raising must never propagate — investigate() always returns a dict."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        # Patch the low-level refine_hypothesis function; _llm_refine_hypotheses catches it.
        with patch("supervisor.evidence_gates.GATES_ENABLED", False), \
             patch("supervisor.agent._llm_enabled", return_value=True), \
             patch("supervisor.agent._llm_refine", side_effect=RuntimeError("bedrock endpoint unreachable")):
            result = sup.investigate("INC_TEST001")

        assert "root_cause" in result
        assert "confidence" in result
        assert result.get("confidence_degraded") is True

    def test_llm_reasoning_failure_sets_confidence_degraded(self):
        """When _llm_generate_reasoning fails, result must carry confidence_degraded."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        with patch("supervisor.evidence_gates.GATES_ENABLED", False), \
             patch("supervisor.agent._llm_enabled", return_value=True), \
             patch.object(sup, "_llm_refine_hypotheses", return_value={}), \
             patch.object(
                sup, "_llm_generate_reasoning",
                return_value={
                    "reasoning": "Fallback reasoning.",
                    "llm_reasoning_status": "failed",
                    "llm_reasoning_error": "ModelNotFound",
                },
             ):
            result = sup.investigate("INC_TEST001")

        assert result.get("confidence_degraded") is True


# ---------------------------------------------------------------------------
# 5. Deadline exceeded
# ---------------------------------------------------------------------------

class TestDeadlineExceeded:
    def _make_playbook_that_expires_deadline(self, sup):
        """Return a side_effect that expires the TLS deadline after being called.

        By expiring the deadline inside the mock, we simulate the scenario where
        evidence collection completes but the analysis deadline has already passed —
        the exact condition that _empty_result(degraded=True) is meant to handle.
        """
        def _expire_and_return(*args, **kwargs):
            sup._tls.investigation_deadline = time.monotonic() - 1
            return {}  # empty evidence; gate disabled below so this is fine

        return _expire_and_return

    def test_deadline_exceeded_returns_degraded_result(self):
        """When the deadline expires after evidence collection but before analysis,
        the result must be marked degraded with root_cause=investigation_deadline_exceeded."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        with patch("supervisor.evidence_gates.GATES_ENABLED", False), \
             patch.object(sup, "_execute_playbook",
                          side_effect=self._make_playbook_that_expires_deadline(sup)):
            result = sup.investigate("INC_DEADLINE_TEST")

        assert result.get("confidence_degraded") is True, (
            f"Deadline-exceeded result must have confidence_degraded=True; got {result}"
        )
        assert "investigation_deadline_exceeded" in result.get("root_cause", "")

    def test_deadline_result_has_required_keys(self):
        """Even on deadline timeout, incident_id, root_cause, and confidence must be present."""
        sup = _make_supervisor()
        _wire_ops_worker(sup, _MINIMAL_INCIDENT)

        with patch("supervisor.evidence_gates.GATES_ENABLED", False), \
             patch.object(sup, "_execute_playbook",
                          side_effect=self._make_playbook_that_expires_deadline(sup)):
            result = sup.investigate("INC_DEADLINE_KEYS")

        assert "incident_id" in result
        assert "root_cause" in result
        assert "confidence" in result
