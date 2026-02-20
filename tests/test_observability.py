"""Tests for structured observability module."""

import pytest

from supervisor.observability import Span, trace_span


class TestSpan:
    def test_basic_span(self):
        s = Span("test_op")
        assert s.name == "test_op"
        assert s.status == "ok"

    def test_set_attribute(self):
        s = Span("test_op")
        s.set_attribute("case_id", "INC1")
        assert s.attributes["case_id"] == "INC1"

    def test_add_event(self):
        s = Span("test_op")
        s.add_event("checkpoint", {"step": 1})
        assert len(s.events) == 1
        assert s.events[0]["name"] == "checkpoint"

    def test_end_records_elapsed(self):
        s = Span("test_op")
        s.end(status="ok")
        assert s.end_time > 0
        assert "elapsed_ms" in s.attributes

    def test_elapsed_ms_before_end(self):
        s = Span("test_op")
        elapsed = s.elapsed_ms
        assert elapsed >= 0


class TestTraceSpan:
    def test_context_manager_success(self):
        with trace_span("test", case_id="INC1") as span:
            span.set_attribute("foo", "bar")
        assert span.status == "ok"
        assert span.attributes["case_id"] == "INC1"

    def test_context_manager_error(self):
        with pytest.raises(ValueError):
            with trace_span("test_err") as span:
                raise ValueError("boom")
        assert span.status == "error"
        assert "boom" in span.attributes.get("error", "")

    def test_nested_spans(self):
        with trace_span("outer", case_id="INC1") as outer:
            with trace_span("inner", case_id="INC1") as inner:
                inner.set_attribute("step", "inner")
        assert outer.status == "ok"
        assert inner.status == "ok"
