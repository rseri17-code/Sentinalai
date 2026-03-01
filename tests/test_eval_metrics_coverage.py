"""
Phase 2 coverage tests for supervisor/eval_metrics.py.

Targets uncovered paths where the OTEL meter IS available:
_get_or_create double-check, _up_down_counter, record_*
functions with a real meter, and record_budget_exhausted /
record_circuit_breaker_trip / record_llm_usage with instruments.
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from supervisor.eval_metrics import (
    _get_or_create,
    _counter,
    _histogram,
    _up_down_counter,
    _confidence_bracket,
    record_investigation,
    record_worker_call,
    record_circuit_breaker_trip,
    record_budget_exhausted,
    record_evidence_completeness,
    record_receipt_summary,
    record_eval_score,
    record_llm_usage,
    record_judge_scores,
    _instruments,
    _instruments_lock,
)


# =========================================================================
# Helpers
# =========================================================================

def _mock_meter():
    """Create a mock meter with counter/histogram/up_down_counter factories."""
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock()
    meter.create_histogram.return_value = MagicMock()
    meter.create_up_down_counter.return_value = MagicMock()
    return meter


# =========================================================================
# _get_or_create with double-check locking
# =========================================================================

class TestGetOrCreate:
    """Cover the double-check locking path in _get_or_create."""

    def test_double_check_returns_cached(self):
        """When instrument is created between first check and lock acquisition,
        the cached version is returned."""
        mock_meter = _mock_meter()
        test_name = "test.double_check_metric"
        # Pre-populate the instrument
        _instruments[test_name] = MagicMock()
        try:
            with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
                result = _get_or_create(test_name, mock_meter.create_counter)
                assert result is _instruments[test_name]
                # Factory should NOT have been called since it was already cached
                mock_meter.create_counter.assert_not_called()
        finally:
            _instruments.pop(test_name, None)

    def test_creates_new_instrument(self):
        """When instrument doesn't exist, creates and caches it."""
        mock_meter = _mock_meter()
        test_name = "test.new_instrument_metric"
        _instruments.pop(test_name, None)  # Ensure not cached
        try:
            with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
                result = _get_or_create(test_name, mock_meter.create_counter, description="test")
                assert result is not None
                mock_meter.create_counter.assert_called_once()
        finally:
            _instruments.pop(test_name, None)

    def test_meter_none_returns_none(self):
        """When meter is None, return None."""
        test_name = "test.no_meter_metric"
        _instruments.pop(test_name, None)
        with patch("supervisor.eval_metrics.get_meter", return_value=None):
            result = _get_or_create(test_name, lambda n, **kw: MagicMock())
            assert result is None


# =========================================================================
# _up_down_counter
# =========================================================================

class TestUpDownCounter:
    """Cover _up_down_counter function."""

    def test_up_down_counter_with_meter(self):
        mock_meter = _mock_meter()
        test_name = "test.updown_metric"
        _instruments.pop(test_name, None)
        try:
            with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
                result = _up_down_counter(test_name, description="test")
                assert result is not None
        finally:
            _instruments.pop(test_name, None)

    def test_up_down_counter_without_meter(self):
        with patch("supervisor.eval_metrics.get_meter", return_value=None):
            result = _up_down_counter("test.no_meter_updown")
            assert result is None


# =========================================================================
# record_* with active meter
# =========================================================================

class TestRecordWithMeter:
    """Cover record_* functions when OTEL meter is available."""

    def setup_method(self):
        """Clear instrument cache before each test."""
        _instruments.clear()

    def test_record_investigation_with_keywords(self):
        """record_investigation with root_cause_keywords_matched > 0."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_investigation(
                incident_id="INC123",
                incident_type="timeout",
                service="api-gateway",
                confidence=85,
                root_cause="database timeout",
                tool_calls=5,
                evidence_sources=3,
                hypothesis_count=2,
                winner_hypothesis="db_timeout",
                elapsed_ms=1500.0,
                root_cause_keywords_matched=3,
            )
            # Should have created counter and histogram instruments
            assert mock_meter.create_counter.call_count >= 1
            assert mock_meter.create_histogram.call_count >= 1

    def test_record_worker_call_with_incident_type(self):
        """record_worker_call with incident_type set."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_worker_call(
                worker_name="log_worker",
                action="search_logs",
                status="success",
                elapsed_ms=250.0,
                incident_type="timeout",
            )

    def test_record_circuit_breaker_trip(self):
        """record_circuit_breaker_trip emits counter."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_circuit_breaker_trip("log_worker", "closed_to_open")

    def test_record_budget_exhausted(self):
        """record_budget_exhausted emits counter + histogram."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_budget_exhausted(
                incident_type="timeout",
                service="api-gateway",
                calls_made=20,
                max_calls=20,
            )

    def test_record_evidence_completeness(self):
        """record_evidence_completeness emits per-source counters."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_evidence_completeness(
                incident_type="error_spike",
                logs_available=True,
                signals_available=True,
                metrics_available=False,
                events_available=True,
                changes_available=False,
            )

    def test_record_receipt_summary_with_failures(self):
        """record_receipt_summary with failed > 0 emits failure counter."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_receipt_summary(
                incident_type="timeout",
                total_calls=10,
                succeeded=8,
                failed=2,
                total_elapsed_ms=5000.0,
            )

    def test_record_eval_score(self):
        """record_eval_score emits histogram."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_eval_score(
                incident_id="INC123",
                incident_type="timeout",
                dimension="root_cause_accuracy",
                score=0.85,
            )

    def test_record_llm_usage_full(self):
        """record_llm_usage emits all GenAI metrics."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_llm_usage(
                operation="refine_hypothesis",
                model_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
                input_tokens=500,
                output_tokens=200,
                latency_ms=1200.0,
                incident_type="timeout",
            )

    def test_record_judge_scores(self):
        """record_judge_scores emits per-dimension histograms."""
        mock_meter = _mock_meter()
        with patch("supervisor.eval_metrics.get_meter", return_value=mock_meter):
            record_judge_scores(
                incident_id="INC123",
                incident_type="timeout",
                scores={
                    "root_cause_accuracy": 0.9,
                    "causal_reasoning": 0.85,
                    "evidence_usage": 0.8,
                    "overall": 0.85,
                },
                source="llm_judge",
            )


# =========================================================================
# _confidence_bracket edge cases
# =========================================================================

class TestConfidenceBracket:
    """Ensure all brackets are covered."""

    def test_very_low(self):
        assert _confidence_bracket(10) == "very_low"
        assert _confidence_bracket(25) == "very_low"

    def test_low(self):
        assert _confidence_bracket(30) == "low"
        assert _confidence_bracket(50) == "low"

    def test_medium(self):
        assert _confidence_bracket(60) == "medium"
        assert _confidence_bracket(75) == "medium"

    def test_high(self):
        assert _confidence_bracket(80) == "high"
        assert _confidence_bracket(90) == "high"

    def test_very_high(self):
        assert _confidence_bracket(95) == "very_high"
        assert _confidence_bracket(100) == "very_high"
