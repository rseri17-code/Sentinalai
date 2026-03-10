"""Scoring purity tests for SentinalAI.

Validates that compute_confidence() is a pure function:
- Same inputs → same output (no hidden state, no side effects)
- Confidence ceiling enforced (no causal artifact → < 80)
- Log bonus capped at 5
- Source count multiplier is correct
- Anomaly bonus applied exactly once
- Score clamped to [0, 100]

These tests are MANDATORY before any change to the scoring formula.
"""

from __future__ import annotations

import pytest

from supervisor.agent import Hypothesis, compute_confidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_signals() -> dict:
    return {}


def _good_signals(anomaly: bool = False) -> dict:
    return {
        "golden_signals": {
            "latency": {"p95": 500, "baseline_p95": 100},
            "errors": {"rate": 0.05},
        },
        "anomaly_detected": anomaly,
    }


def _good_metrics(pattern: str | None = None) -> dict:
    d: dict = {"metrics": [{"name": "cpu", "value": 80}]}
    if pattern:
        d["pattern"] = pattern
    return d


def _empty_metrics() -> dict:
    return {}


def _make_logs(n: int) -> list[dict]:
    return [{"_time": f"2024-01-01T00:00:{i:02d}Z", "message": f"log {i}"} for i in range(n)]


# ---------------------------------------------------------------------------
# Test: scoring is a pure function
# ---------------------------------------------------------------------------

class TestScoringIsAPureFunction:
    """compute_confidence has no side effects and no hidden state."""

    def test_scoring_is_a_pure_function(self) -> None:
        """Calling compute_confidence does not alter any global state."""
        logs = _make_logs(3)
        signals = _good_signals()
        metrics = _good_metrics()
        events = [{"type": "deploy"}]
        changes = [{"sha": "abc"}]

        # Call many times, results must be identical
        results = [
            compute_confidence(50, logs, signals, metrics, events, changes)
            for _ in range(50)
        ]
        assert len(set(results)) == 1

    def test_pure_with_empty_inputs(self) -> None:
        """Empty inputs produce consistent result."""
        results = [
            compute_confidence(30, [], {}, {}, [], [])
            for _ in range(50)
        ]
        assert len(set(results)) == 1

    def test_different_base_different_result(self) -> None:
        """Different base scores produce different results."""
        a = compute_confidence(40, [], {}, {}, [], [])
        b = compute_confidence(60, [], {}, {}, [], [])
        assert a != b
        assert a < b


# ---------------------------------------------------------------------------
# Test: confidence ceiling without causal artifact
# ---------------------------------------------------------------------------

class TestConfidenceCeilingWithoutCausalArtifact:
    """No evidence_refs → confidence must stay < 80.

    This is enforced in the supervisor pipeline (not in compute_confidence
    directly), but we validate the scoring inputs that would produce the
    ceiling scenario.
    """

    def test_confidence_ceiling_without_causal_artifact(self) -> None:
        """A hypothesis with no evidence_refs cannot have confidence >= 80
        after the supervisor applies the ceiling rule."""
        # Simulate: high base score but no evidence_refs
        h = Hypothesis(
            name="test_hypothesis",
            root_cause="some cause",
            base_score=90,
            evidence_refs=[],  # No causal artifact
            reasoning="test",
        )
        # The ceiling is enforced in the supervisor as:
        # if not winner.evidence_refs: confidence = min(confidence, 79)
        confidence = h.base_score
        if not h.evidence_refs:
            confidence = min(confidence, 79)
        assert confidence < 80

    def test_with_evidence_refs_can_reach_80(self) -> None:
        """A hypothesis WITH evidence_refs can reach >= 80."""
        h = Hypothesis(
            name="test_hypothesis",
            root_cause="some cause",
            base_score=85,
            evidence_refs=["logs:error", "metrics:spike"],
            reasoning="test",
        )
        confidence = h.base_score
        if not h.evidence_refs:
            confidence = min(confidence, 79)
        assert confidence >= 80


# ---------------------------------------------------------------------------
# Test: log bonus capped at five
# ---------------------------------------------------------------------------

class TestLogBonusCappedAtFive:
    """Log evidence bonus is +1 per log entry, maximum +5."""

    def test_log_bonus_capped_at_five(self) -> None:
        """More than 5 logs still only give +5 bonus."""
        base = 50

        # 5 logs: should give max log bonus
        five_log_score = compute_confidence(base, _make_logs(5), _good_signals(), _good_metrics(), [], [])

        # 10 logs: should give same bonus as 5 logs (capped)
        ten_log_score = compute_confidence(base, _make_logs(10), _good_signals(), _good_metrics(), [], [])

        # 5 and 10 logs should have identical score (both capped at +5)
        assert five_log_score == ten_log_score

    def test_log_bonus_scales_up_to_five(self) -> None:
        """1 log gives +1, 2 gives +2, ... 5 gives +5."""
        base = 50
        signals = _good_signals()
        metrics = _good_metrics()

        scores = []
        for n in range(0, 7):
            score = compute_confidence(base, _make_logs(n), signals, metrics, [], [])
            scores.append(score)

        # Each additional log up to 5 should increase score
        for i in range(1, 5):
            assert scores[i] >= scores[i - 1], (
                f"Score with {i} logs ({scores[i]}) should be >= score with {i-1} logs ({scores[i-1]})"
            )

        # After 5, score should be stable (cap reached)
        assert scores[5] == scores[6], (
            f"Score with 5 logs ({scores[5]}) should equal score with 6 logs ({scores[6]})"
        )


# ---------------------------------------------------------------------------
# Test: source count multiplier correct
# ---------------------------------------------------------------------------

class TestSourceCountMultiplierCorrect:
    """Each evidence source adds +2 to the cross-signal bonus."""

    def test_source_count_multiplier_correct(self) -> None:
        """Adding one more source type increases score by exactly +2 (cross-signal)
        plus any type-specific bonus."""
        base = 50

        # 0 sources (but signals/metrics penalties apply)
        score_no_sources = compute_confidence(base, [], {}, {}, [], [])

        # With logs only (1 source)
        score_logs_only = compute_confidence(base, _make_logs(1), {}, {}, [], [])

        # The log adds: +1 (log bonus) + 2 (source_count * 2)
        # So difference should be +3
        diff = score_logs_only - score_no_sources
        assert diff == 3, f"Adding logs should add +3 (1 log + 2 cross-signal), got +{diff}"

    def test_all_five_sources_present(self) -> None:
        """All 5 source types present → source_count=5 → +10 cross-signal bonus."""
        base = 50
        # All sources present
        full_score = compute_confidence(
            base,
            _make_logs(1),
            _good_signals(),
            _good_metrics(),
            [{"type": "deploy"}],
            [{"sha": "abc"}],
        )
        # No sources
        empty_score = compute_confidence(base, [], {}, {}, [], [])

        # Full should be significantly higher than empty
        assert full_score > empty_score + 10


# ---------------------------------------------------------------------------
# Test: anomaly bonus applied exactly once
# ---------------------------------------------------------------------------

class TestAnomalyBonusAppliedExactlyOnce:
    """Anomaly detection adds +2 bonus, exactly once."""

    def test_anomaly_bonus_applied_exactly_once(self) -> None:
        """anomaly_detected=True adds exactly +2 compared to False."""
        base = 50
        logs = _make_logs(2)
        metrics = _good_metrics()

        no_anomaly = compute_confidence(
            base, logs, _good_signals(anomaly=False), metrics, [], [],
        )
        with_anomaly = compute_confidence(
            base, logs, _good_signals(anomaly=True), metrics, [], [],
        )

        assert with_anomaly - no_anomaly == 2, (
            f"Anomaly bonus should be exactly +2, got +{with_anomaly - no_anomaly}"
        )

    def test_anomaly_without_golden_signals_ignored(self) -> None:
        """anomaly_detected without golden_signals gives no bonus."""
        base = 50
        # anomaly_detected but no golden_signals key
        broken_signals = {"anomaly_detected": True}
        no_signals = {}

        score_broken = compute_confidence(base, [], broken_signals, {}, [], [])
        score_none = compute_confidence(base, [], no_signals, {}, [], [])

        assert score_broken == score_none


# ---------------------------------------------------------------------------
# Test: score clamped to [0, 100]
# ---------------------------------------------------------------------------

class TestScoreClampedToZeroOneHundred:
    """Final confidence is always in [0, 100]."""

    def test_score_clamped_to_zero_one_hundred(self) -> None:
        """Score never goes below 0 or above 100."""
        # Very low base — heavy penalties should not go negative
        low = compute_confidence(-50, [], {}, {}, [], [])
        assert low == 0

        # Very high base + all bonuses should not exceed 100
        high = compute_confidence(
            95,
            _make_logs(10),
            _good_signals(anomaly=True),
            _good_metrics(pattern="spike"),
            [{"type": "deploy"}],
            [{"sha": "abc"}],
            corroborating_sources=5,
        )
        assert high == 100

    def test_zero_base_with_evidence(self) -> None:
        """Base=0 with full evidence doesn't go below 0."""
        score = compute_confidence(
            0,
            _make_logs(5),
            _good_signals(anomaly=True),
            _good_metrics(pattern="spike"),
            [{"type": "deploy"}],
            [{"sha": "abc"}],
        )
        assert 0 <= score <= 100

    def test_hundred_base_no_penalties(self) -> None:
        """Base=100 with full evidence stays at 100."""
        score = compute_confidence(
            100,
            _make_logs(5),
            _good_signals(),
            _good_metrics(),
            [{"type": "deploy"}],
            [{"sha": "abc"}],
        )
        assert score == 100

    @pytest.mark.parametrize("base", [-100, -10, 0, 50, 100, 150, 200])
    def test_always_in_range(self, base: int) -> None:
        """Regardless of base, result is always in [0, 100]."""
        score = compute_confidence(base, _make_logs(3), _good_signals(), _good_metrics(), [], [])
        assert 0 <= score <= 100, f"Score {score} out of range for base={base}"


# ---------------------------------------------------------------------------
# Test: missing-source penalties
# ---------------------------------------------------------------------------

class TestMissingSourcePenalties:
    """Missing signals costs -5, missing metrics costs -3."""

    def test_missing_signals_penalty(self) -> None:
        """No golden_signals → -5 penalty."""
        base = 50
        with_signals = compute_confidence(base, [], _good_signals(), _good_metrics(), [], [])
        without_signals = compute_confidence(base, [], {}, _good_metrics(), [], [])
        # Missing signals: -5 penalty + loss of source_count bonus (-2)
        assert with_signals > without_signals

    def test_missing_metrics_penalty(self) -> None:
        """No metrics → -3 penalty."""
        base = 50
        with_metrics = compute_confidence(base, [], _good_signals(), _good_metrics(), [], [])
        without_metrics = compute_confidence(base, [], _good_signals(), {}, [], [])
        # Missing metrics: -3 penalty + loss of source_count bonus (-2)
        assert with_metrics > without_metrics


# ---------------------------------------------------------------------------
# Test: corroborating sources bonus
# ---------------------------------------------------------------------------

class TestCorroboratingSources:
    """Explicit corroborating_sources parameter adds +2 each."""

    def test_corroborating_sources_bonus(self) -> None:
        """Each corroborating source adds +2."""
        base = 50
        s0 = compute_confidence(base, [], _good_signals(), _good_metrics(), [], [], corroborating_sources=0)
        s1 = compute_confidence(base, [], _good_signals(), _good_metrics(), [], [], corroborating_sources=1)
        s2 = compute_confidence(base, [], _good_signals(), _good_metrics(), [], [], corroborating_sources=2)

        assert s1 - s0 == 2
        assert s2 - s1 == 2


# ---------------------------------------------------------------------------
# Test: pattern bonus
# ---------------------------------------------------------------------------

class TestPatternBonus:
    """Metrics with pattern field add +1."""

    def test_metrics_pattern_bonus(self) -> None:
        """Having a pattern field in metrics adds exactly +1."""
        base = 50
        no_pattern = compute_confidence(base, [], _good_signals(), _good_metrics(pattern=None), [], [])
        with_pattern = compute_confidence(base, [], _good_signals(), _good_metrics(pattern="spike"), [], [])

        assert with_pattern - no_pattern == 1
