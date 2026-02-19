"""Structured observability for SentinalAI.

Provides OTEL-native tracing with graceful fallback to lightweight
structured logging when the SDK is not configured or unavailable.

When OTEL_EXPORTER_OTLP_ENDPOINT is set, spans are exported via OTLP
to the collector (which routes to Splunk HEC).  Otherwise spans are
logged as structured JSON — zero-cost in tests, full fidelity in prod.

GenAI semantic conventions (gen_ai.*) are applied to agent investigation
spans so Splunk dashboards and eval pipelines can query them natively.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger("sentinalai.trace")

# =========================================================================
# OTEL SDK integration (graceful — never fail if SDK absent)
# =========================================================================

_tracer = None  # real OTEL tracer, or None
_meter = None   # real OTEL meter, or None

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    _otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    if _otlp_endpoint:
        _resource = Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "sentinalai"),
            "service.version": "0.1.0",
        })

        # Traces
        _trace_provider = TracerProvider(resource=_resource)
        _trace_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp_endpoint, insecure=True))
        )
        otel_trace.set_tracer_provider(_trace_provider)
        _tracer = otel_trace.get_tracer("sentinalai", "0.1.0")

        # Metrics
        _metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=_otlp_endpoint, insecure=True),
            export_interval_millis=10_000,
        )
        _meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
        otel_metrics.set_meter_provider(_meter_provider)
        _meter = otel_metrics.get_meter("sentinalai", "0.1.0")

        logger.info("OTEL tracing + metrics enabled -> %s", _otlp_endpoint)
    else:
        logger.debug("OTEL_EXPORTER_OTLP_ENDPOINT not set; using lightweight spans")

except ImportError:
    logger.debug("opentelemetry SDK not installed; using lightweight spans")


def get_meter():
    """Return the OTEL meter (or None if SDK not configured)."""
    return _meter


# =========================================================================
# GenAI semantic convention attribute keys
# https://opentelemetry.io/docs/specs/semconv/gen-ai/
# =========================================================================

GENAI_SYSTEM = "gen_ai.system"
GENAI_REQUEST_MODEL = "gen_ai.request.model"
GENAI_RESPONSE_MODEL = "gen_ai.response.model"
GENAI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GENAI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GENAI_OPERATION_NAME = "gen_ai.operation.name"


# =========================================================================
# Agent eval attribute keys (custom namespace for SentinalAI evals)
# =========================================================================

EVAL_INCIDENT_TYPE = "sentinalai.incident_type"
EVAL_SERVICE = "sentinalai.service"
EVAL_CONFIDENCE = "sentinalai.confidence"
EVAL_ROOT_CAUSE = "sentinalai.root_cause"
EVAL_TOOL_CALLS = "sentinalai.tool_calls"
EVAL_HYPOTHESIS_COUNT = "sentinalai.hypothesis_count"
EVAL_WINNER_NAME = "sentinalai.winner_hypothesis"
EVAL_EVIDENCE_SOURCES = "sentinalai.evidence_sources"
EVAL_BUDGET_REMAINING = "sentinalai.budget_remaining"
EVAL_CIRCUIT_OPEN = "sentinalai.circuit_breakers_open"


# =========================================================================
# Lightweight Span (fallback when OTEL SDK is absent)
# =========================================================================

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


# =========================================================================
# trace_span — unified context manager (OTEL-native or lightweight)
# =========================================================================

@contextmanager
def trace_span(
    name: str,
    case_id: str = "",
    **attributes: Any,
) -> Generator[Span, None, None]:
    """Context manager that creates a span and logs on exit.

    When the OTEL SDK is configured, a real OTEL span is created in
    parallel so traces flow to the collector / Splunk.  The lightweight
    Span is always returned to the caller for attribute setting (the
    OTEL span mirrors those attributes on exit).

    Usage:
        with trace_span("investigate", case_id="INC123") as span:
            span.set_attribute("incident_type", "timeout")
            ...  # do work
    """
    # Always create the lightweight span (used by callers and tests)
    span = Span(name, {"case_id": case_id, **attributes})

    # Optionally create a real OTEL span
    otel_span = None
    otel_ctx = None
    if _tracer is not None:
        otel_ctx = _tracer.start_as_current_span(
            name,
            attributes={"case_id": case_id, **attributes},
        )
        otel_span = otel_ctx.__enter__()

    try:
        yield span
    except Exception as exc:
        span.set_attribute("error", str(exc))
        span.end(status="error")
        _log_span(span)
        if otel_span is not None:
            otel_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            _mirror_to_otel(span, otel_span)
            otel_ctx.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        span.end(status="ok")
        _log_span(span)
        if otel_span is not None:
            otel_span.set_status(otel_trace.StatusCode.OK)
            _mirror_to_otel(span, otel_span)
            otel_ctx.__exit__(None, None, None)


# =========================================================================
# Internal helpers
# =========================================================================

def _mirror_to_otel(span: Span, otel_span: Any) -> None:
    """Copy lightweight span attributes/events onto the real OTEL span."""
    for key, value in span.attributes.items():
        if isinstance(value, (str, int, float, bool)):
            otel_span.set_attribute(key, value)
    for event in span.events:
        safe_attrs = {
            k: v for k, v in event.get("attributes", {}).items()
            if isinstance(v, (str, int, float, bool))
        }
        otel_span.add_event(event["name"], attributes=safe_attrs)


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
