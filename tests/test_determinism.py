"""Determinism tests for SentinalAI.

Validates that the deterministic pipeline produces identical results
for the same input, regardless of ordering, timing, or LLM state.

These tests are MANDATORY before any change to:
- supervisor/tool_selector.py (classifier or playbooks)
- supervisor/agent.py (hypothesis scoring, tiebreak, analyzers)
- compute_confidence() scoring formula
"""

from __future__ import annotations

import time

import pytest

from supervisor.agent import Hypothesis, compute_confidence
from supervisor.tool_selector import (
    CLASSIFICATION_KEYWORDS,
    VALID_INCIDENT_TYPES,
    classify_incident,
    get_playbook,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure LLM is disabled for determinism tests."""
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("CALIBRATION_ENABLED", "false")


def _make_hypothesis(name: str, score: float, refs: list[str] | None = None) -> Hypothesis:
    return Hypothesis(
        name=name,
        root_cause=f"root cause for {name}",
        base_score=score,
        evidence_refs=refs or [],
        reasoning=f"reasoning for {name}",
    )


# Standard evidence inputs for compute_confidence reproducibility
STANDARD_LOGS: list[dict] = [
    {"_time": "2024-01-01T00:00:00Z", "message": "error one"},
    {"_time": "2024-01-01T00:00:01Z", "message": "error two"},
    {"_time": "2024-01-01T00:00:02Z", "message": "error three"},
]

STANDARD_SIGNALS: dict = {
    "golden_signals": {
        "latency": {"p95": 500, "baseline_p95": 100},
        "errors": {"rate": 0.05},
    },
    "anomaly_detected": True,
}

STANDARD_METRICS: dict = {
    "metrics": [{"name": "cpu", "value": 80}],
    "pattern": "spike",
}

STANDARD_EVENTS: list[dict] = [
    {"type": "deployment", "timestamp": "2024-01-01T00:00:00Z"},
]

STANDARD_CHANGES: list[dict] = [
    {"sha": "abc123", "message": "deploy v2.1"},
]


# ---------------------------------------------------------------------------
# Test: same incident always produces the same incident type
# ---------------------------------------------------------------------------

class TestSameIncidentProducesSameIncidentType:
    """classify_incident is deterministic for keyword-based classification."""

    @pytest.mark.parametrize("summary,expected", [
        ("API Gateway timeout spike", "timeout"),
        ("OOMKill in user-service pod", "oomkill"),
        ("Error spike after deployment", "error_spike"),
        ("High latency on search-service", "latency"),
        ("CPU saturation on order-service", "saturation"),
        ("Network connectivity failure", "network"),
        ("Cascading failure across services", "cascading"),
        ("Missing data in telemetry pipeline", "missing_data"),
        ("Flapping alerts on auth-service", "flapping"),
        ("Throughput drop on queue processor", "silent_failure"),
    ])
    def test_same_incident_produces_same_incident_type(self, summary: str, expected: str) -> None:
        """Same summary text → same incident type, every time."""
        results = [classify_incident(summary) for _ in range(100)]
        assert all(r == expected for r in results), (
            f"Non-deterministic classification for '{summary}': {set(results)}"
        )

    def test_all_10_types_reachable(self) -> None:
        """Every incident type has at least one keyword that reaches it."""
        for incident_type, keywords in CLASSIFICATION_KEYWORDS.items():
            result = classify_incident(keywords[0])
            assert result == incident_type, (
                f"Keyword '{keywords[0]}' should classify as '{incident_type}', got '{result}'"
            )

    def test_classification_case_insensitive(self) -> None:
        """Classification is case-insensitive."""
        assert classify_incident("TIMEOUT on api-gateway") == classify_incident("timeout on api-gateway")
        assert classify_incident("OOM in pod") == classify_incident("oom in pod")


# ---------------------------------------------------------------------------
# Test: same incident produces same hypothesis winner
# ---------------------------------------------------------------------------

class TestSameIncidentProducesSameHypothesisWinner:
    """Hypothesis selection is deterministic given same hypotheses."""

    def test_same_incident_produces_same_hypothesis_winner(self) -> None:
        """Sort by (-score, name) always selects the same winner."""
        hypotheses = [
            _make_hypothesis("cpu_exhaustion", 78, ["logs:cpu_spike"]),
            _make_hypothesis("memory_leak", 76, ["metrics:memory_growth"]),
            _make_hypothesis("disk_pressure", 60),
        ]
        for _ in range(100):
            import random
            random.shuffle(hypotheses)
            hypotheses.sort(key=lambda h: (-h.base_score, h.name))
            assert hypotheses[0].name == "cpu_exhaustion"

    def test_tiebreak_is_alphabetical(self) -> None:
        """When scores are equal, alphabetically first name wins."""
        hypotheses = [
            _make_hypothesis("zebra_hypothesis", 80),
            _make_hypothesis("alpha_hypothesis", 80),
            _make_hypothesis("mid_hypothesis", 80),
        ]
        hypotheses.sort(key=lambda h: (-h.base_score, h.name))
        assert hypotheses[0].name == "alpha_hypothesis"


# ---------------------------------------------------------------------------
# Test: same incident produces same confidence score
# ---------------------------------------------------------------------------

class TestSameIncidentProducesSameConfidenceScore:
    """compute_confidence is a pure function — same inputs → same output."""

    def test_same_incident_produces_same_confidence_score(self) -> None:
        """Identical inputs produce identical confidence, 100 times."""
        results = [
            compute_confidence(
                base=80,
                logs=STANDARD_LOGS,
                signals=STANDARD_SIGNALS,
                metrics=STANDARD_METRICS,
                events=STANDARD_EVENTS,
                changes=STANDARD_CHANGES,
            )
            for _ in range(100)
        ]
        assert len(set(results)) == 1, f"Non-deterministic confidence: {set(results)}"

    def test_confidence_stable_across_calls(self) -> None:
        """Two separate calls with same args produce same result."""
        a = compute_confidence(50, STANDARD_LOGS, STANDARD_SIGNALS, STANDARD_METRICS, STANDARD_EVENTS, STANDARD_CHANGES)
        b = compute_confidence(50, STANDARD_LOGS, STANDARD_SIGNALS, STANDARD_METRICS, STANDARD_EVENTS, STANDARD_CHANGES)
        assert a == b


# ---------------------------------------------------------------------------
# Test: same incident produces same tool call sequence
# ---------------------------------------------------------------------------

class TestSameIncidentProducesSameToolCallSequence:
    """Playbook steps are always the same for a given incident type."""

    def test_same_incident_produces_same_tool_call_sequence(self) -> None:
        """get_playbook returns same steps for same incident type, always."""
        for incident_type in VALID_INCIDENT_TYPES:
            playbooks = [get_playbook(incident_type) for _ in range(50)]
            first = playbooks[0]
            for i, pb in enumerate(playbooks[1:], 1):
                assert pb == first, (
                    f"Playbook for '{incident_type}' changed on call {i}: {pb} != {first}"
                )

    def test_all_playbooks_are_lists(self) -> None:
        """Every incident type has a list-type playbook."""
        for incident_type in VALID_INCIDENT_TYPES:
            pb = get_playbook(incident_type)
            assert isinstance(pb, list), f"Playbook for {incident_type} is not a list"
            assert len(pb) > 0, f"Playbook for {incident_type} is empty"


# ---------------------------------------------------------------------------
# Test: alphabetical tiebreak on equal scores
# ---------------------------------------------------------------------------

class TestAlphabeticalTiebreakOnEqualScores:
    """Tiebreak guarantees deterministic winner when scores are identical."""

    def test_alphabetical_tiebreak_on_equal_scores(self) -> None:
        """Two hypotheses with same score → alphabetically first wins."""
        pairs = [
            ("alpha", "beta"),
            ("cpu_leak", "memory_leak"),
            ("dns_failure", "network_partition"),
        ]
        for name_a, name_b in pairs:
            hypotheses = [
                _make_hypothesis(name_b, 75),
                _make_hypothesis(name_a, 75),
            ]
            hypotheses.sort(key=lambda h: (-h.base_score, h.name))
            assert hypotheses[0].name == name_a, (
                f"Expected '{name_a}' to win tiebreak over '{name_b}'"
            )

    def test_three_way_tiebreak(self) -> None:
        """Three hypotheses at same score → alphabetical winner."""
        hypotheses = [
            _make_hypothesis("charlie", 70),
            _make_hypothesis("alice", 70),
            _make_hypothesis("bob", 70),
        ]
        hypotheses.sort(key=lambda h: (-h.base_score, h.name))
        assert [h.name for h in hypotheses] == ["alice", "bob", "charlie"]

    def test_tiebreak_stable_after_shuffle(self) -> None:
        """Shuffling input order doesn't change tiebreak result."""
        import random
        names = ["zulu", "alpha", "mike", "bravo"]
        for _ in range(50):
            hypotheses = [_make_hypothesis(n, 60) for n in names]
            random.shuffle(hypotheses)
            hypotheses.sort(key=lambda h: (-h.base_score, h.name))
            assert hypotheses[0].name == "alpha"


# ---------------------------------------------------------------------------
# Test: LLM disabled does not change winner
# ---------------------------------------------------------------------------

class TestLlmDisabledDoesNotChangeWinner:
    """With LLM disabled, deterministic path is the only path."""

    def test_llm_disabled_does_not_change_winner(self) -> None:
        """classify_incident returns same result with LLM_ENABLED=false."""
        for incident_type, keywords in CLASSIFICATION_KEYWORDS.items():
            for kw in keywords[:2]:  # test first 2 keywords per type
                result = classify_incident(kw)
                assert result == incident_type

    def test_default_fallback_without_llm(self) -> None:
        """Unknown summary falls back to error_spike when LLM is off."""
        result = classify_incident("xyzzy completely unknown incident type 999")
        assert result == "error_spike"


# ---------------------------------------------------------------------------
# Test: clock independence
# ---------------------------------------------------------------------------

class TestClockIndependence:
    """Scoring and classification do not depend on wall clock time."""

    def test_clock_independence(self) -> None:
        """compute_confidence output is identical regardless of current time."""
        result_before = compute_confidence(
            75, STANDARD_LOGS, STANDARD_SIGNALS, STANDARD_METRICS,
            STANDARD_EVENTS, STANDARD_CHANGES,
        )
        # Simulate time passage
        time.sleep(0.01)
        result_after = compute_confidence(
            75, STANDARD_LOGS, STANDARD_SIGNALS, STANDARD_METRICS,
            STANDARD_EVENTS, STANDARD_CHANGES,
        )
        assert result_before == result_after

    def test_classification_clock_independent(self) -> None:
        """classify_incident returns same result regardless of time."""
        r1 = classify_incident("API Gateway timeout spike")
        time.sleep(0.01)
        r2 = classify_incident("API Gateway timeout spike")
        assert r1 == r2 == "timeout"


# ---------------------------------------------------------------------------
# Test: 100-run determinism (nightly-style)
# ---------------------------------------------------------------------------

class TestHundredRunDeterminism:
    """Run the full classification + scoring 100 times for each type."""

    @pytest.mark.parametrize("incident_type", sorted(VALID_INCIDENT_TYPES))
    def test_100_run_classification(self, incident_type: str) -> None:
        """Classify the first keyword 100 times — always same result."""
        kw = CLASSIFICATION_KEYWORDS[incident_type][0]
        results = {classify_incident(kw) for _ in range(100)}
        assert results == {incident_type}

    def test_100_run_confidence(self) -> None:
        """compute_confidence 100 times with same input — always same."""
        results = {
            compute_confidence(65, STANDARD_LOGS, STANDARD_SIGNALS,
                               STANDARD_METRICS, STANDARD_EVENTS, STANDARD_CHANGES)
            for _ in range(100)
        }
        assert len(results) == 1
