"""
RCA output contract tests.

These tests enforce the SCHEMA and INVARIANTS of every investigation output.
If the output shape changes, these tests fail — catching regressions before
they reach consumers (dashboards, Slack notifications, PagerDuty runbooks).
"""

import pytest
from unittest.mock import Mock, MagicMock

from supervisor.agent import SentinalAISupervisor
from tests.fixtures.mock_mcp_responses import ALL_MOCKS
from tests.fixtures.expected_rca_outputs import EXPECTED_RCA
from tests.test_supervisor import _build_mock_workers


INCIDENT_IDS = list(ALL_MOCKS.keys())


# =========================================================================
# Schema enforcement
# =========================================================================

class TestRCAOutputSchema:
    """Every investigation must produce output matching the exact contract."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_output_has_all_required_keys(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)

        required = {"incident_id", "root_cause", "confidence", "evidence_timeline", "reasoning"}
        missing = required - set(result.keys())
        assert not missing, f"{incident_id}: missing keys {missing}"

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_incident_id_matches_input(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        assert result["incident_id"] == incident_id

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_root_cause_is_nonempty_string(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        assert isinstance(result["root_cause"], str)
        assert len(result["root_cause"]) > 0

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_confidence_is_int_0_to_100(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        c = result["confidence"]
        assert isinstance(c, int), f"confidence must be int, got {type(c)}"
        assert 0 <= c <= 100, f"confidence {c} out of [0, 100]"

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_evidence_timeline_is_list_of_dicts(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        timeline = result["evidence_timeline"]
        assert isinstance(timeline, list)
        for i, entry in enumerate(timeline):
            assert isinstance(entry, dict), f"timeline[{i}] is {type(entry)}, expected dict"

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_timeline_entries_have_required_fields(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        for i, entry in enumerate(result["evidence_timeline"]):
            assert "timestamp" in entry, f"timeline[{i}] missing 'timestamp'"
            assert "event" in entry, f"timeline[{i}] missing 'event'"
            assert "source" in entry, f"timeline[{i}] missing 'source'"

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_reasoning_minimum_length(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        assert isinstance(result["reasoning"], str)
        assert len(result["reasoning"]) >= 50, (
            f"{incident_id}: reasoning too short ({len(result['reasoning'])} chars)"
        )


# =========================================================================
# Timeline invariants
# =========================================================================

class TestTimelineInvariants:
    """Timeline must be chronologically ordered and non-trivial."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_timeline_chronologically_ordered(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        timeline = result["evidence_timeline"]
        timestamps = [e.get("timestamp", "") for e in timeline]
        for i in range(len(timestamps) - 1):
            assert timestamps[i] <= timestamps[i + 1], (
                f"{incident_id}: timeline out of order at index {i}: "
                f"{timestamps[i]} > {timestamps[i + 1]}"
            )

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_timeline_has_minimum_events(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        timeline = result["evidence_timeline"]
        assert len(timeline) >= 2, (
            f"{incident_id}: timeline has {len(timeline)} events, need >= 2"
        )

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_timeline_sources_are_valid(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        valid_sources = {
            "golden_signals", "metrics", "events", "logs",
            "log_summary", "changes",
        }
        for entry in result["evidence_timeline"]:
            assert entry.get("source") in valid_sources, (
                f"{incident_id}: invalid source '{entry.get('source')}'"
            )

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_timeline_events_are_nonempty_strings(self, incident_id):
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        for i, entry in enumerate(result["evidence_timeline"]):
            assert isinstance(entry["event"], str)
            assert len(entry["event"]) > 0, (
                f"{incident_id}: timeline[{i}] has empty event"
            )


# =========================================================================
# Confidence invariants
# =========================================================================

class TestConfidenceInvariants:
    """Confidence must reflect data quality."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    def test_unknown_incident_has_low_confidence(self):
        """No data at all -> confidence must be <= 50."""
        _build_mock_workers(self.supervisor, "INC_UNKNOWN")
        result = self.supervisor.investigate("INC_UNKNOWN")
        assert result["confidence"] <= 50

    def test_empty_workers_have_low_confidence(self):
        """All workers returning {} -> confidence must be <= 50."""
        for name in self.supervisor.workers:
            self.supervisor.workers[name] = MagicMock()
            self.supervisor.workers[name].execute = Mock(return_value={})
        result = self.supervisor.investigate("INC12345")
        assert result["confidence"] <= 50

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_confidence_matches_expected_range(self, incident_id):
        expected = EXPECTED_RCA[incident_id]
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"], (
            f"{incident_id}: confidence {result['confidence']} outside "
            f"[{expected['confidence_min']}, {expected['confidence_max']}]"
        )


# =========================================================================
# Reasoning quality invariants
# =========================================================================

class TestReasoningQuality:
    """Reasoning must explain the root cause, not just restate it."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_reasoning_longer_than_root_cause(self, incident_id):
        """Reasoning should be more detailed than the root cause one-liner."""
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        assert len(result["reasoning"]) > len(result["root_cause"]), (
            f"{incident_id}: reasoning should be longer than root_cause"
        )

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_reasoning_mentions_service(self, incident_id):
        """Reasoning should reference the affected service."""
        _build_mock_workers(self.supervisor, incident_id)
        result = self.supervisor.investigate(incident_id)
        incident = ALL_MOCKS[incident_id]["moogsoft.get_incident_by_id"]
        service = incident["affected_service"]
        # The reasoning should mention the service or a related component
        reasoning_lower = result["reasoning"].lower()
        assert (
            service.lower() in reasoning_lower
            or any(
                part in reasoning_lower
                for part in service.lower().split("-")
                if len(part) > 2
            )
        ), f"{incident_id}: reasoning doesn't mention service '{service}'"


# =========================================================================
# Determinism contract
# =========================================================================

class TestDeterminismContract:
    """Same input must always produce identical output — across ALL incidents."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    @pytest.mark.parametrize("incident_id", INCIDENT_IDS)
    def test_deterministic_output(self, incident_id):
        """Run investigation 3 times, output must be identical."""
        results = []
        for _ in range(3):
            _build_mock_workers(self.supervisor, incident_id)
            result = self.supervisor.investigate(incident_id)
            results.append(result)

        for i in range(1, len(results)):
            assert results[0]["root_cause"] == results[i]["root_cause"], (
                f"{incident_id}: root_cause differs between run 0 and {i}"
            )
            assert results[0]["confidence"] == results[i]["confidence"], (
                f"{incident_id}: confidence differs between run 0 and {i}"
            )
            assert results[0]["reasoning"] == results[i]["reasoning"], (
                f"{incident_id}: reasoning differs between run 0 and {i}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
