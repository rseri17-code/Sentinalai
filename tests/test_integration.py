"""
Integration tests for SentinalAI.
Tests end-to-end investigation flow, edge cases, and performance.
"""

import time

import pytest
from unittest.mock import Mock, MagicMock

from supervisor.agent import SentinalAISupervisor
from tests.fixtures.mock_mcp_responses import ALL_MOCKS
from tests.fixtures.expected_rca_outputs import EXPECTED_RCA
from tests.test_supervisor import _build_mock_workers


class TestEndToEndInvestigation:
    """Full investigation flow from incident ID to RCA output."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_investigation_returns_required_fields(self):
        """Every investigation must return root_cause, confidence, evidence_timeline, reasoning."""
        _build_mock_workers(self.supervisor, "INC12345")
        result = self.supervisor.investigate("INC12345")

        required_fields = ["root_cause", "confidence", "evidence_timeline", "reasoning"]
        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

    def test_confidence_is_numeric(self):
        """Confidence must be a number between 0 and 100."""
        _build_mock_workers(self.supervisor, "INC12345")
        result = self.supervisor.investigate("INC12345")

        assert isinstance(result["confidence"], (int, float))
        assert 0 <= result["confidence"] <= 100

    def test_evidence_timeline_is_list(self):
        """evidence_timeline must be a list."""
        _build_mock_workers(self.supervisor, "INC12345")
        result = self.supervisor.investigate("INC12345")

        assert isinstance(result["evidence_timeline"], list)

    def test_reasoning_is_nonempty_string(self):
        """reasoning must be a non-empty string."""
        _build_mock_workers(self.supervisor, "INC12345")
        result = self.supervisor.investigate("INC12345")

        assert isinstance(result["reasoning"], str)
        assert len(result["reasoning"]) > 50


class TestPerformance:
    """Verify all investigations complete within the time budget."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    @pytest.mark.parametrize("incident_id", list(ALL_MOCKS.keys()))
    def test_investigation_under_60_seconds(self, incident_id):
        """Each investigation must complete in under 60 seconds."""
        _build_mock_workers(self.supervisor, incident_id)

        start = time.time()
        result = self.supervisor.investigate(incident_id)
        elapsed = time.time() - start

        assert elapsed <= 60, (
            f"{incident_id} took {elapsed:.1f}s (max 60s)"
        )
        assert "root_cause" in result


class TestEdgeCases:
    """Edge-case and error-handling tests."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_unknown_incident_id(self):
        """Unknown incident should return a result with low confidence, not crash."""
        # Wire up empty mocks
        _build_mock_workers(self.supervisor, "INC_UNKNOWN")
        result = self.supervisor.investigate("INC_UNKNOWN")

        assert "root_cause" in result
        assert "confidence" in result
        # Should not be high confidence with no data
        assert result["confidence"] <= 50

    def test_worker_returning_empty(self):
        """If all workers return empty data, investigation should still complete."""
        for name in self.supervisor.workers:
            self.supervisor.workers[name] = MagicMock()
            self.supervisor.workers[name].execute = Mock(return_value={})

        result = self.supervisor.investigate("INC12345")

        assert "root_cause" in result
        assert "confidence" in result

    def test_partial_worker_failure(self):
        """If one worker raises, investigation should degrade gracefully."""
        _build_mock_workers(self.supervisor, "INC12345")

        # Make metrics worker raise
        def exploding_metrics(action, params):
            raise ConnectionError("Sysdig unavailable")

        self.supervisor.workers["metrics_worker"].execute = Mock(side_effect=exploding_metrics)

        result = self.supervisor.investigate("INC12345")

        assert "root_cause" in result
        assert "confidence" in result
        # Confidence might be lower but should still produce output


class TestToolSelection:
    """Verify intelligent tool selection (not loading all 89 tools)."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_timeout_incident_selects_right_tools(self):
        """Timeout investigation should call ops, logs, metrics, APM workers."""
        _build_mock_workers(self.supervisor, "INC12345")
        self.supervisor.investigate("INC12345")

        assert self.supervisor.workers["ops_worker"].execute.called
        assert self.supervisor.workers["log_worker"].execute.called

    def test_oomkill_incident_selects_right_tools(self):
        """OOMKill investigation should call ops, logs, metrics workers."""
        _build_mock_workers(self.supervisor, "INC12346")
        self.supervisor.investigate("INC12346")

        assert self.supervisor.workers["ops_worker"].execute.called
        assert self.supervisor.workers["log_worker"].execute.called
        assert self.supervisor.workers["metrics_worker"].execute.called


class TestOutputQuality:
    """Validate the quality of RCA outputs across all incidents."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    @pytest.mark.parametrize("incident_id", list(ALL_MOCKS.keys()))
    def test_root_cause_keywords(self, incident_id):
        """Root cause must contain expected keywords."""
        expected = EXPECTED_RCA[incident_id]
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, (
                f"{incident_id}: root cause missing keyword '{keyword}'. "
                f"Got: {result['root_cause']}"
            )

    @pytest.mark.parametrize("incident_id", list(ALL_MOCKS.keys()))
    def test_confidence_in_range(self, incident_id):
        """Confidence must fall within expected range."""
        expected = EXPECTED_RCA[incident_id]
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"], (
            f"{incident_id}: confidence {result['confidence']} outside "
            f"[{expected['confidence_min']}, {expected['confidence_max']}]"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
