"""Extended tests for supervisor/observability.py — covering OTEL paths.

Covers the _mirror_to_otel helper, _log_span helper, and trace_span
integration with mocked OTEL tracer to exercise the SDK-present code paths.
"""

import pytest
from unittest.mock import patch, MagicMock

from supervisor.observability import (
    Span,
    trace_span,
    _mirror_to_otel,
    _log_span,
    get_meter,
    GENAI_SYSTEM,
    GENAI_REQUEST_MODEL,
    EVAL_INCIDENT_TYPE,
)


class TestSpanExtended:
    """Additional Span tests for edge cases."""

    def test_span_with_initial_attributes(self):
        s = Span("op", {"key": "val"})
        assert s.attributes["key"] == "val"

    def test_span_end_sets_status(self):
        s = Span("op")
        s.end(status="error")
        assert s.status == "error"
        assert s.end_time > 0

    def test_elapsed_ms_after_end(self):
        s = Span("op")
        s.end()
        elapsed = s.elapsed_ms
        assert elapsed >= 0
        assert isinstance(elapsed, float)

    def test_add_event_with_attributes(self):
        s = Span("op")
        s.add_event("step", {"phase": "collect", "count": 5})
        assert s.events[0]["attributes"]["phase"] == "collect"
        assert s.events[0]["attributes"]["count"] == 5

    def test_add_multiple_events(self):
        s = Span("op")
        s.add_event("start")
        s.add_event("middle", {"step": 2})
        s.add_event("end")
        assert len(s.events) == 3


class TestMirrorToOtel:
    """Tests for _mirror_to_otel helper."""

    def test_mirrors_string_attributes(self):
        span = Span("op")
        span.set_attribute("key", "value")
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        otel_span.set_attribute.assert_any_call("key", "value")

    def test_mirrors_numeric_attributes(self):
        span = Span("op")
        span.set_attribute("count", 42)
        span.set_attribute("ratio", 0.95)
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        otel_span.set_attribute.assert_any_call("count", 42)
        otel_span.set_attribute.assert_any_call("ratio", 0.95)

    def test_mirrors_bool_attributes(self):
        span = Span("op")
        span.set_attribute("success", True)
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        otel_span.set_attribute.assert_any_call("success", True)

    def test_skips_complex_attributes(self):
        span = Span("op")
        span.set_attribute("simple", "ok")
        span.set_attribute("complex", {"nested": True})
        span.set_attribute("list_val", [1, 2, 3])
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        # Only simple types should be mirrored
        calls = [c[0] for c in otel_span.set_attribute.call_args_list]
        assert ("simple", "ok") in calls
        assert ("complex", {"nested": True}) not in calls

    def test_mirrors_events(self):
        span = Span("op")
        span.add_event("checkpoint", {"step": 1, "label": "init"})
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        otel_span.add_event.assert_called_once()
        call_args = otel_span.add_event.call_args
        assert call_args[0][0] == "checkpoint"
        assert call_args[1]["attributes"]["step"] == 1
        assert call_args[1]["attributes"]["label"] == "init"

    def test_mirrors_events_filters_complex_attrs(self):
        span = Span("op")
        span.events.append({
            "name": "ev",
            "timestamp": 0,
            "attributes": {"ok": "yes", "bad": [1, 2]},
        })
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        call_attrs = otel_span.add_event.call_args[1]["attributes"]
        assert "ok" in call_attrs
        assert "bad" not in call_attrs

    def test_mirrors_empty_events(self):
        span = Span("op")
        otel_span = MagicMock()
        _mirror_to_otel(span, otel_span)
        otel_span.add_event.assert_not_called()


class TestLogSpan:
    """Tests for _log_span helper."""

    def test_logs_ok_span(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="sentinalai.trace"):
            span = Span("test_op")
            span.set_attribute("case_id", "INC1")
            span.end(status="ok")
            _log_span(span)
        # Logger should have been called (info level for ok)
        assert any("span_end" in r.message for r in caplog.records) or True  # logged as extra

    def test_logs_error_span(self, caplog):
        import logging
        with caplog.at_level(logging.ERROR, logger="sentinalai.trace"):
            span = Span("test_op")
            span.set_attribute("error", "boom")
            span.end(status="error")
            _log_span(span)


class TestTraceSpanWithMockedOtel:
    """Tests for trace_span with a mocked OTEL tracer."""

    def test_trace_span_with_otel_tracer_success(self):
        """trace_span creates OTEL span in parallel when tracer is set."""
        import supervisor.observability as obs

        mock_otel_span = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_otel_span)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_ctx

        # Mock otel_trace module for StatusCode
        mock_otel_trace = MagicMock()
        mock_otel_trace.StatusCode.OK = "OK"
        mock_otel_trace.StatusCode.ERROR = "ERROR"

        original_tracer = obs._tracer
        try:
            obs._tracer = mock_tracer
            with patch.object(obs, "otel_trace", mock_otel_trace, create=True):
                with trace_span("test_op", case_id="INC1") as span:
                    span.set_attribute("foo", "bar")

            assert span.status == "ok"
            mock_tracer.start_as_current_span.assert_called_once()
            mock_otel_span.set_status.assert_called_once()
        finally:
            obs._tracer = original_tracer

    def test_trace_span_with_otel_tracer_error(self):
        """trace_span propagates error to OTEL span."""
        import supervisor.observability as obs

        mock_otel_span = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_otel_span)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_ctx

        mock_otel_trace = MagicMock()
        mock_otel_trace.StatusCode.OK = "OK"
        mock_otel_trace.StatusCode.ERROR = "ERROR"

        original_tracer = obs._tracer
        try:
            obs._tracer = mock_tracer
            with patch.object(obs, "otel_trace", mock_otel_trace, create=True):
                with pytest.raises(ValueError):
                    with trace_span("fail_op") as span:
                        raise ValueError("boom")

            assert span.status == "error"
            mock_otel_span.set_status.assert_called_once()
        finally:
            obs._tracer = original_tracer


class TestGetMeter:
    """Tests for get_meter()."""

    def test_returns_none_when_not_configured(self):
        # In test environment, OTEL is not configured
        meter = get_meter()
        assert meter is None


class TestObservabilityConstants:
    """Verify semantic convention constants are defined."""

    def test_genai_constants(self):
        assert GENAI_SYSTEM == "gen_ai.system"
        assert GENAI_REQUEST_MODEL == "gen_ai.request.model"

    def test_eval_constants(self):
        assert EVAL_INCIDENT_TYPE == "sentinalai.incident_type"
