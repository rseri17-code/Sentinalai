"""
Comprehensive test suite for SentinalAI supervisor.
Tests define production quality - code must pass ALL tests.
"""

import time

import pytest
from unittest.mock import Mock, MagicMock

from supervisor.agent import SentinalAISupervisor
from tests.fixtures.mock_mcp_responses import ALL_MOCKS
from tests.fixtures.expected_rca_outputs import EXPECTED_RCA


# =============================================================================
# Helper: build mock workers from ALL_MOCKS for a given incident
# =============================================================================

def _build_mock_workers(supervisor, incident_id):
    """
    Replace every worker on *supervisor* with a Mock whose `.execute()`
    returns the correct fixture data based on *incident_id*.
    """
    mocks = ALL_MOCKS.get(incident_id, {})

    # ---- ops_worker (Moogsoft) ----
    def mock_ops(action, params):
        if action == "get_incident_by_id":
            return {"incident": mocks.get("moogsoft.get_incident_by_id", {})}
        return {}

    # ---- log_worker (Splunk) ----
    def mock_logs(action, params):
        query = (params.get("query") or "").lower()

        # Fallback: try change data
        if action == "get_change_data":
            for key, value in mocks.items():
                if "change_data" in key or "app_change" in key:
                    return value
            return {"changes": []}

        # Build a list of (keyword, mock_key_substring) matchers.
        # Order matters: more specific keywords first to avoid greedy matching.
        keyword_matchers = [
            ("pipeline", "pipeline"),
            ("elasticsearch", "elasticsearch"),
            ("timeout", "timeout"),
            ("oomkill", "oomkill"),
            ("oom", "oomkill"),
            ("memory", "memory"),
            ("latency", "latency"),
            ("cpu", "cpu"),
            ("network", "network"),
            ("dns", "dns"),
            ("cascade", "cascade"),
            ("notification", "notification"),
            ("auth", "auth"),
            ("recommendation", "recommendation"),
        ]

        # First pass: find the best keyword match
        for query_kw, key_kw in keyword_matchers:
            if query_kw not in query:
                continue
            for key, value in mocks.items():
                if not key.startswith("splunk.search"):
                    continue
                if key_kw in key:
                    return {"logs": value}

        # Second pass: match error queries (avoid matching change data)
        if "error" in query:
            for key, value in mocks.items():
                if key.startswith("splunk.search") and "error" in key:
                    return {"logs": value}

        # Final fallback: return first splunk.search result
        for key, value in mocks.items():
            if key.startswith("splunk.search"):
                return {"logs": value}

        return {}

    # ---- metrics_worker (Sysdig metrics / events) ----
    def mock_metrics(action, params):
        service = (params.get("service") or params.get("target") or "").lower()

        if action in ("query_metrics", "get_resource_metrics"):
            for key, value in mocks.items():
                if key.startswith("sysdig.query_metrics") and service in key:
                    return {"metrics": value}
            # Fallback: return first query_metrics
            for key, value in mocks.items():
                if key.startswith("sysdig.query_metrics"):
                    return {"metrics": value}

        if action == "get_events":
            for key, value in mocks.items():
                if key.startswith("sysdig.get_events") and service in key:
                    return value
            for key, value in mocks.items():
                if key.startswith("sysdig.get_events"):
                    return value

        return {}

    # ---- apm_worker (Sysdig golden signals) ----
    def mock_apm(action, params):
        service = (params.get("service") or params.get("target") or "").lower()

        if action in ("check_latency", "get_golden_signals"):
            for key, value in mocks.items():
                if key.startswith("sysdig.golden_signals") and service in key:
                    return {"signals": value}
            # Fallback: return first golden signals
            for key, value in mocks.items():
                if key.startswith("sysdig.golden_signals"):
                    return {"signals": value}

        return {}

    # ---- knowledge_worker ----
    def mock_knowledge(action, params):
        return {"similar_incidents": []}

    # Patch workers
    for name in ("ops_worker", "log_worker", "metrics_worker", "apm_worker", "knowledge_worker"):
        if name not in supervisor.workers:
            supervisor.workers[name] = MagicMock()

    supervisor.workers["ops_worker"].execute = Mock(side_effect=mock_ops)
    supervisor.workers["log_worker"].execute = Mock(side_effect=mock_logs)
    supervisor.workers["metrics_worker"].execute = Mock(side_effect=mock_metrics)
    supervisor.workers["apm_worker"].execute = Mock(side_effect=mock_apm)
    supervisor.workers["knowledge_worker"].execute = Mock(side_effect=mock_knowledge)


# =============================================================================
# Test class
# =============================================================================

class TestSupervisorWithMocks:
    """Test supervisor against realistic mock MCP responses."""

    def setup_method(self):
        self.supervisor = SentinalAISupervisor()

    # --------------------------------------------------------------------- #
    # Helpers
    # --------------------------------------------------------------------- #

    def _run(self, incident_id):
        """Run an investigation with mocks wired for *incident_id*."""
        _build_mock_workers(self.supervisor, incident_id)
        start = time.time()
        result = self.supervisor.investigate(incident_id)
        elapsed = time.time() - start
        return result, elapsed

    # ===================================================================== #
    # TEST 1: Timeout Incident (INC12345)
    # ===================================================================== #

    def test_timeout_incident_INC12345(self):
        """
        Test investigation of timeout incident.

        SUCCESS CRITERIA:
        - Root cause matches expected keywords
        - Confidence within range
        - Timeline chronologically correct
        - Reasoning explains causality
        - Investigation completes in < 60s
        """
        expected = EXPECTED_RCA["INC12345"]
        result, elapsed = self._run("INC12345")

        # 1. Root cause correctness
        assert "root_cause" in result, "Result must include root_cause field"
        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, (
                f"Root cause must mention '{keyword}'. Got: {result['root_cause']}"
            )

        # 2. Confidence level
        assert "confidence" in result, "Result must include confidence field"
        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"], (
            f"Confidence {result['confidence']}% outside expected range "
            f"[{expected['confidence_min']}-{expected['confidence_max']}%]"
        )

        # 3. Required evidence present
        evidence_text = str(result.get("evidence_timeline", [])).lower()
        for req in expected["required_evidence"]:
            assert req.lower() in evidence_text, f"Missing required evidence: '{req}'"

        # 4. Timeline correctness
        timeline = result.get("evidence_timeline", [])
        assert len(timeline) >= 2, "Timeline must have at least 2 events"

        first_event = str(timeline[0]).lower()
        assert "latency" in first_event or "slow" in first_event, (
            "First timeline event should be latency-related"
        )

        timestamps = [e["timestamp"] for e in timeline if "timestamp" in e]
        for i in range(len(timestamps) - 1):
            assert timestamps[i] <= timestamps[i + 1], "Timeline must be chronologically ordered"

        # 5. Reasoning quality
        assert "reasoning" in result, "Result must include reasoning"
        reasoning = result["reasoning"].lower()
        assert len(reasoning) > 100, "Reasoning must be substantial (>100 chars)"

        causality_kw = ["cause", "caused", "led to", "result", "precede"]
        assert any(kw in reasoning for kw in causality_kw), "Reasoning must explain causality"

        timeline_kw = ["timeline", "first", "before", "preceded"]
        assert any(kw in reasoning for kw in timeline_kw), "Reasoning must reference timeline"

        # 6. Investigation speed
        assert elapsed <= expected["investigation_time_max_seconds"], (
            f"Investigation took {elapsed:.1f}s, max is {expected['investigation_time_max_seconds']}s"
        )

    # ===================================================================== #
    # TEST 2: OOMKill Incident (INC12346)
    # ===================================================================== #

    def test_oomkill_incident_INC12346(self):
        """Test investigation of OOMKill incident."""
        expected = EXPECTED_RCA["INC12346"]
        result, elapsed = self._run("INC12346")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

        evidence_text = str(result.get("evidence_timeline", [])).lower()
        for req in expected["required_evidence"]:
            assert req.lower() in evidence_text, f"Missing evidence: '{req}'"

        reasoning = result["reasoning"].lower()
        assert any(kw in reasoning for kw in ("gradual", "increasing", "leak")), (
            "Must identify memory leak pattern"
        )

        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 3: Error Spike After Deployment (INC12347)
    # ===================================================================== #

    def test_error_spike_deployment_INC12347(self):
        """Test investigation of error spike after deployment."""
        result, _ = self._run("INC12347")

        root_cause = result["root_cause"].lower()
        assert "deployment" in root_cause, "Must correlate with deployment"
        assert "nullpointer" in root_cause or "exception" in root_cause, (
            "Must identify specific error type"
        )

        evidence_text = str(result).lower()
        assert "deployment" in evidence_text or "change" in evidence_text

        reasoning = result["reasoning"].lower()
        assert "deployment" in reasoning
        assert any(kw in reasoning for kw in ("before", "precede", "after", "introduced"))

    # ===================================================================== #
    # TEST 4: Latency Incident (INC12348)
    # ===================================================================== #

    def test_latency_incident_INC12348(self):
        """Test investigation of latency incident."""
        expected = EXPECTED_RCA["INC12348"]
        result, elapsed = self._run("INC12348")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]
        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 5: Resource Saturation (INC12349)
    # ===================================================================== #

    def test_resource_saturation_INC12349(self):
        """Test investigation of resource saturation."""
        expected = EXPECTED_RCA["INC12349"]
        result, elapsed = self._run("INC12349")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

        evidence_text = str(result).lower()
        assert "cpu" in evidence_text or "saturation" in evidence_text
        assert "config" in evidence_text or "change" in evidence_text

        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 6: Network Issue (INC12350)
    # ===================================================================== #

    def test_network_issue_INC12350(self):
        """Test investigation of network issue."""
        expected = EXPECTED_RCA["INC12350"]
        result, elapsed = self._run("INC12350")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]
        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 7: Complex Multi-Cause Incident (INC12351)
    # ===================================================================== #

    def test_cascading_failure_INC12351(self):
        """Test investigation of cascading failure."""
        expected = EXPECTED_RCA["INC12351"]
        result, elapsed = self._run("INC12351")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

        # Must show cascade understanding
        reasoning = result["reasoning"].lower()
        assert any(kw in reasoning for kw in ("cascade", "cascading", "downstream", "propagat"))

        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 8: Missing Data Scenario (INC12352)
    # ===================================================================== #

    def test_missing_data_INC12352(self):
        """Test investigation with partial/missing data."""
        expected = EXPECTED_RCA["INC12352"]
        result, elapsed = self._run("INC12352")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        # Lower confidence is acceptable for missing data
        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 9: Flapping Alerts (INC12353)
    # ===================================================================== #

    def test_flapping_alerts_INC12353(self):
        """Test investigation of intermittent/flapping alerts."""
        expected = EXPECTED_RCA["INC12353"]
        result, elapsed = self._run("INC12353")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

        reasoning = result["reasoning"].lower()
        assert any(kw in reasoning for kw in ("intermittent", "flapping", "pattern", "sawtooth", "periodic"))

        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # TEST 10: Silent Failure (INC12354)
    # ===================================================================== #

    def test_silent_failure_INC12354(self):
        """Test investigation of silent failure / throughput drop."""
        expected = EXPECTED_RCA["INC12354"]
        result, elapsed = self._run("INC12354")

        root_cause = result["root_cause"].lower()
        for keyword in expected["root_cause_keywords"]:
            assert keyword.lower() in root_cause, f"Root cause must mention '{keyword}'"

        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

        reasoning = result["reasoning"].lower()
        assert any(kw in reasoning for kw in ("pipeline", "stale", "upstream", "indirect"))

        assert elapsed <= expected["investigation_time_max_seconds"]

    # ===================================================================== #
    # META TEST: All Incidents Must Pass
    # ===================================================================== #

    def test_all_incidents_pass(self):
        """Meta-test: every test incident must produce a valid RCA."""
        incidents = list(ALL_MOCKS.keys())
        failures = []

        for incident_id in incidents:
            expected = EXPECTED_RCA[incident_id]
            _build_mock_workers(self.supervisor, incident_id)

            try:
                result = self.supervisor.investigate(incident_id)
                passed = (
                    "root_cause" in result
                    and "confidence" in result
                    and result["confidence"] >= expected["confidence_min"]
                    and all(
                        kw.lower() in result["root_cause"].lower()
                        for kw in expected["root_cause_keywords"]
                    )
                )
                if not passed:
                    failures.append(
                        f"{incident_id}: keywords or confidence mismatch "
                        f"(confidence={result.get('confidence')}, "
                        f"root_cause={result.get('root_cause', '')!r})"
                    )
            except Exception as exc:
                failures.append(f"{incident_id}: {exc}")

        assert not failures, "Failed incidents:\n" + "\n".join(failures)

    # ===================================================================== #
    # Determinism
    # ===================================================================== #

    def test_deterministic_investigation(self):
        """Same input must produce identical output."""
        results = []
        for _ in range(3):
            _build_mock_workers(self.supervisor, "INC12345")
            result = self.supervisor.investigate("INC12345")
            results.append(result["root_cause"])

        assert results[0] == results[1] == results[2], "Investigation must be deterministic"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
