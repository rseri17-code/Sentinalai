"""Structured observability for SentinalAI.

Provides OTEL-ready structured logging and trace context
without requiring a full OTEL SDK dependency at import time.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger("sentinalai.trace")


class Span:
    """Lightweight span for tracing a unit of work.

    Mirrors OTEL span interface so it can be swapped for real OTEL spans
    when the SDK is available.
    """

    def __init__(self, name: str, attributes: dict[str, Any] | None = None):
        self.name = name
        self.attributes: dict[str, Any] = attributes or {}
        self.start_time = time.monotonic()
        self.end_time: float = 0.0
        self.status = "ok"
        self.events: list[dict] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.monotonic(),
            "attributes": attributes or {},
        })

    def end(self, status: str = "ok") -> None:
        self.end_time = time.monotonic()
        self.status = status
        elapsed_ms = (self.end_time - self.start_time) * 1000
        self.attributes["elapsed_ms"] = round(elapsed_ms, 1)

    @property
    def elapsed_ms(self) -> float:
        if self.end_time:
            return round((self.end_time - self.start_time) * 1000, 1)
        return round((time.monotonic() - self.start_time) * 1000, 1)


@contextmanager
def trace_span(
    name: str,
    case_id: str = "",
    **attributes: Any,
) -> Generator[Span, None, None]:
    """Context manager that creates a span and logs on exit.

    Usage:
        with trace_span("investigate", case_id="INC123") as span:
            span.set_attribute("incident_type", "timeout")
            ...  # do work
    """
    span = Span(name, {"case_id": case_id, **attributes})
    try:
        yield span
    except Exception as exc:
        span.set_attribute("error", str(exc))
        span.end(status="error")
        _log_span(span)
        raise
    else:
        span.end(status="ok")
        _log_span(span)


def _log_span(span: Span) -> None:
    """Emit a structured log line for the span."""
    log_data = {
        "span": span.name,
        "status": span.status,
        "elapsed_ms": span.elapsed_ms,
        **{k: v for k, v in span.attributes.items() if k != "elapsed_ms"},
    }
    if span.status == "error":
        logger.error("span_end", extra=log_data)
    else:
        logger.info("span_end", extra=log_data)
