"""
Phase 2 coverage tests for supervisor/observability.py.

Targets the OTEL SDK initialization path (lines 34-71) and
trace_span OTEL integration paths.
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
import os

from supervisor.observability import (
    Span,
    trace_span,
    _log_span,
    get_meter,
    GENAI_SYSTEM,
    EVAL_CONFIDENCE,
)


# =========================================================================
# OTEL SDK initialization path
# =========================================================================

class TestOtelSdkInitialization:
    """Cover OTEL SDK init when OTEL_EXPORTER_OTLP_ENDPOINT is set.

    These paths (lines 34-71) are module-level code that runs at import.
    We test the behavior indirectly through the public API.
    """

    def test_get_meter_returns_none_when_no_endpoint(self):
        """Without OTEL endpoint, get_meter() returns None."""
        # By default in tests, OTEL_EXPORTER_OTLP_ENDPOINT is not set
        meter = get_meter()
        # In test environment, meter is None (no endpoint configured)
        assert meter is None

    def test_tracer_is_none_when_no_endpoint(self):
        """Without OTEL endpoint, _tracer is None."""
        from supervisor.observability import _tracer
        assert _tracer is None

    def test_trace_span_with_otel_tracer_success(self):
        """When _tracer is set, OTEL span is created alongside lightweight span."""
        import supervisor.observability as obs_mod

        mock_otel_span = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_otel_span)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_ctx

        mock_otel_trace = MagicMock()
        mock_otel_trace.StatusCode.OK = "OK"

        orig_tracer = obs_mod._tracer
        orig_otel_trace = getattr(obs_mod, "otel_trace", None)
        try:
            obs_mod._tracer = mock_tracer
            obs_mod.otel_trace = mock_otel_trace

            with trace_span("test_span", case_id="INC123") as span:
                span.set_attribute("test_key", "test_value")

            mock_tracer.start_as_current_span.assert_called_once()
            mock_otel_span.set_status.assert_called_once()
        finally:
            obs_mod._tracer = orig_tracer
            if orig_otel_trace is None:
                delattr(obs_mod, "otel_trace") if hasattr(obs_mod, "otel_trace") else None
            else:
                obs_mod.otel_trace = orig_otel_trace

    def test_trace_span_with_otel_tracer_exception(self):
        """When _tracer is set and exception occurs, OTEL span records error."""
        import supervisor.observability as obs_mod

        mock_otel_span = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_otel_span)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value = mock_ctx

        mock_otel_trace = MagicMock()
        mock_otel_trace.StatusCode.ERROR = "ERROR"

        orig_tracer = obs_mod._tracer
        orig_otel_trace = getattr(obs_mod, "otel_trace", None)
        try:
            obs_mod._tracer = mock_tracer
            obs_mod.otel_trace = mock_otel_trace

            with pytest.raises(ValueError):
                with trace_span("test_span") as span:
                    raise ValueError("test error")

            mock_otel_span.set_status.assert_called_once()
        finally:
            obs_mod._tracer = orig_tracer
            if orig_otel_trace is None:
                delattr(obs_mod, "otel_trace") if hasattr(obs_mod, "otel_trace") else None
            else:
                obs_mod.otel_trace = orig_otel_trace

    def test_mirror_to_otel_copies_attributes(self):
        """_mirror_to_otel copies span attributes to OTEL span."""
        from supervisor.observability import _mirror_to_otel
        span = Span("test")
        span.set_attribute("str_attr", "value")
        span.set_attribute("int_attr", 42)
        span.set_attribute("float_attr", 3.14)
        span.set_attribute("bool_attr", True)
        span.add_event("test_event", {"event_key": "event_value"})

        mock_otel_span = MagicMock()
        _mirror_to_otel(span, mock_otel_span)

        # Should have set all 4 attributes + elapsed_ms, case_id
        assert mock_otel_span.set_attribute.call_count >= 4
        mock_otel_span.add_event.assert_called_once()

    def test_mirror_to_otel_skips_non_primitives(self):
        """_mirror_to_otel skips non-primitive attribute values."""
        from supervisor.observability import _mirror_to_otel
        span = Span("test")
        span.set_attribute("list_attr", [1, 2, 3])  # Should be skipped
        span.set_attribute("dict_attr", {"key": "value"})  # Should be skipped
        span.set_attribute("str_attr", "kept")  # Should be kept

        mock_otel_span = MagicMock()
        _mirror_to_otel(span, mock_otel_span)

        # Only str_attr and case_id should be set (primitives only)
        set_attrs = {call.args[0]: call.args[1] for call in mock_otel_span.set_attribute.call_args_list}
        assert "str_attr" in set_attrs
        assert "list_attr" not in set_attrs
        assert "dict_attr" not in set_attrs
