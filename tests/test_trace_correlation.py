"""Tests for supervisor.trace_correlation."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("TRACE_CORRELATION_ENABLED", "true")

from supervisor.trace_correlation import (
    correlate_traces,
    extract_trace_id_from_text,
    _extract_trace_id,
    _build_call_chain,
    _find_error_span,
    _root_service,
    _correlation_confidence,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TRACE_ID_32 = "abc123def456789012345678901234ab"
TRACE_ID_16 = "abc123def456789a"
TRACE_ID_UUID = "abc12345-def4-5678-9012-abcdef123456"

SAMPLE_SPANS = [
    {
        "span_id": "s1",
        "parent_span_id": None,
        "service_name": "client",
        "operation_name": "checkout",
        "duration_ms": 5200,
        "start_time": "2024-01-15T14:02:08.000Z",
        "error": "",
    },
    {
        "span_id": "s2",
        "parent_span_id": "s1",
        "service_name": "payment-service",
        "operation_name": "process_payment",
        "duration_ms": 5180,
        "start_time": "2024-01-15T14:02:08.020Z",
        "error": "",
    },
    {
        "span_id": "s3",
        "parent_span_id": "s2",
        "service_name": "auth-service",
        "operation_name": "session_lookup",
        "duration_ms": 4820,
        "start_time": "2024-01-15T14:02:08.040Z",
        "error": "connection_timeout",
    },
]


# ---------------------------------------------------------------------------
# extract_trace_id_from_text
# ---------------------------------------------------------------------------

class TestExtractTraceIdFromText:

    def test_extracts_32_char_hex(self):
        text = f"Error trace_id={TRACE_ID_32} in payment"
        assert extract_trace_id_from_text(text) == TRACE_ID_32

    def test_extracts_16_char_hex(self):
        text = f"trace={TRACE_ID_16} request failed"
        result = extract_trace_id_from_text(text)
        assert result == TRACE_ID_16

    def test_extracts_uuid(self):
        text = f"X-Trace-ID: {TRACE_ID_UUID}"
        result = extract_trace_id_from_text(text)
        assert result is not None
        assert TRACE_ID_UUID.replace("-", "").startswith(result.replace("-", ""))

    def test_returns_none_on_no_match(self):
        assert extract_trace_id_from_text("no trace here") is None

    def test_returns_none_empty_string(self):
        assert extract_trace_id_from_text("") is None

    def test_case_insensitive(self):
        text = f"TRACE_ID={TRACE_ID_32.upper()}"
        result = extract_trace_id_from_text(text)
        assert result is not None


# ---------------------------------------------------------------------------
# _extract_trace_id
# ---------------------------------------------------------------------------

class TestExtractTraceId:

    def test_from_direct_incident_field(self):
        incident = {"trace_id": TRACE_ID_32, "summary": "payment timeout"}
        result = _extract_trace_id(incident, {})
        assert result == TRACE_ID_32

    def test_from_traceId_camelCase(self):
        incident = {"traceId": TRACE_ID_16}
        assert _extract_trace_id(incident, {}) == TRACE_ID_16

    def test_from_nested_metadata(self):
        incident = {"metadata": {"x-trace-id": TRACE_ID_32}}
        assert _extract_trace_id(incident, {}) == TRACE_ID_32

    def test_from_apm_data(self):
        evidence = {"apm_data": {"trace_id": TRACE_ID_32}}
        assert _extract_trace_id({}, evidence) == TRACE_ID_32

    def test_from_apm_error_samples(self):
        evidence = {
            "apm_data": {
                "error_samples": [
                    {"trace_id": TRACE_ID_32, "message": "timeout"},
                ]
            }
        }
        assert _extract_trace_id({}, evidence) == TRACE_ID_32

    def test_from_summary_text(self):
        incident = {"summary": f"Payment timed out — trace {TRACE_ID_32} captured in APM"}
        assert _extract_trace_id(incident, {}) == TRACE_ID_32

    def test_from_log_entries(self):
        evidence = {
            "logs": {
                "logs": [{"_raw": f"ERROR pool.exhausted trace={TRACE_ID_32}"}]
            }
        }
        assert _extract_trace_id({}, evidence) == TRACE_ID_32

    def test_returns_none_when_not_found(self):
        assert _extract_trace_id({"summary": "no trace here"}, {}) is None

    def test_properties_alias(self):
        incident = {"properties": {"dd-trace-id": TRACE_ID_32}}
        assert _extract_trace_id(incident, {}) == TRACE_ID_32


# ---------------------------------------------------------------------------
# _build_call_chain
# ---------------------------------------------------------------------------

class TestBuildCallChain:

    def test_empty_spans_returns_empty(self):
        assert _build_call_chain([]) == []

    def test_normalises_fields(self):
        spans = [{"service_name": "svc-a", "operation_name": "op1", "duration_ms": 100}]
        chain = _build_call_chain(spans)
        assert chain[0]["service"] == "svc-a"
        assert chain[0]["operation"] == "op1"
        assert chain[0]["duration_ms"] == 100.0

    def test_sorted_by_start_time(self):
        spans = [
            {"service_name": "b", "start_time": "2024-01-15T14:02:08.020Z", "duration_ms": 100},
            {"service_name": "a", "start_time": "2024-01-15T14:02:08.000Z", "duration_ms": 200},
        ]
        chain = _build_call_chain(spans)
        assert chain[0]["service"] == "a"
        assert chain[1]["service"] == "b"

    def test_handles_missing_start_time(self):
        spans = [
            {"service_name": "svc1", "duration_ms": 50},
            {"service_name": "svc2", "duration_ms": 100},
        ]
        chain = _build_call_chain(spans)
        assert len(chain) == 2

    def test_alternate_field_names(self):
        spans = [{"service": "svc-b", "operation": "op2", "duration": 200}]
        chain = _build_call_chain(spans)
        assert chain[0]["service"] == "svc-b"
        assert chain[0]["duration_ms"] == 200.0


# ---------------------------------------------------------------------------
# _find_error_span
# ---------------------------------------------------------------------------

class TestFindErrorSpan:

    def test_returns_none_when_no_errors(self):
        chain = [{"service": "svc", "duration_ms": 100, "error": ""}]
        assert _find_error_span(chain) is None

    def test_returns_error_span(self):
        chain = [
            {"service": "svc-a", "duration_ms": 100, "error": ""},
            {"service": "svc-b", "duration_ms": 4800, "error": "timeout"},
        ]
        span = _find_error_span(chain)
        assert span["service"] == "svc-b"

    def test_returns_slowest_error_span(self):
        chain = [
            {"service": "svc-a", "duration_ms": 100, "error": "err1"},
            {"service": "svc-b", "duration_ms": 4800, "error": "err2"},
            {"service": "svc-c", "duration_ms": 200, "error": "err3"},
        ]
        span = _find_error_span(chain)
        assert span["service"] == "svc-b"  # slowest error


# ---------------------------------------------------------------------------
# _correlation_confidence
# ---------------------------------------------------------------------------

class TestCorrelationConfidence:

    def test_no_trace_id_returns_zero(self):
        assert _correlation_confidence("", [], None) == 0.0

    def test_baseline_with_trace_id(self):
        score = _correlation_confidence(TRACE_ID_32, [], None)
        assert score == 0.5

    def test_chain_depth_bonus(self):
        chain = [{"service": f"svc{i}"} for i in range(4)]
        score = _correlation_confidence(TRACE_ID_32, chain, None)
        assert score >= 0.7  # 0.5 + 0.2 + 0.1

    def test_error_span_bonus(self):
        chain = [{"service": "svc", "error": "timeout"}]
        error_span = {"service": "svc"}
        score = _correlation_confidence(TRACE_ID_32, chain, error_span)
        assert score >= 0.7

    def test_capped_at_1(self):
        chain = [{"service": f"s{i}"} for i in range(10)]
        error_span = {"service": "s0"}
        score = _correlation_confidence(TRACE_ID_32, chain, error_span)
        assert score <= 1.0


# ---------------------------------------------------------------------------
# correlate_traces — integration
# ---------------------------------------------------------------------------

class TestCorrelateTraces:

    def test_returns_none_when_no_trace_id(self):
        incident = {"summary": "payment is slow"}
        result = correlate_traces(incident, {}, gateway=None)
        assert result is None

    def test_returns_correlation_from_incident_field(self):
        incident = {
            "trace_id": TRACE_ID_32,
            "summary": "payment timeout",
            "affected_service": "payment-service",
        }
        evidence = {"apm_data": {"spans": SAMPLE_SPANS}}
        result = correlate_traces(incident, evidence, gateway=None)
        assert result is not None
        assert result["trace_id"] == TRACE_ID_32
        assert "call_chain" in result
        assert len(result["call_chain"]) == len(SAMPLE_SPANS)

    def test_identifies_error_span(self):
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": SAMPLE_SPANS}}
        result = correlate_traces(incident, evidence, gateway=None)
        assert result["error_span"] is not None
        assert result["error_span"]["service"] == "auth-service"
        assert result["error_span"]["error"] == "connection_timeout"

    def test_cross_service_impact(self):
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": SAMPLE_SPANS}}
        result = correlate_traces(incident, evidence, gateway=None)
        services = set(result["cross_service_impact"])
        assert "client" in services
        assert "payment-service" in services
        assert "auth-service" in services

    def test_uses_gateway_when_no_apm_in_evidence(self):
        mock_gw = MagicMock()
        mock_gw.invoke.return_value = {"spans": SAMPLE_SPANS}
        incident = {"trace_id": TRACE_ID_32}
        result = correlate_traces(incident, {}, gateway=mock_gw)
        assert result is not None
        mock_gw.invoke.assert_called_once()

    def test_chain_depth_in_result(self):
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": SAMPLE_SPANS}}
        result = correlate_traces(incident, evidence, gateway=None)
        assert result["chain_depth"] == len(SAMPLE_SPANS)

    def test_disabled_returns_none(self, monkeypatch):
        import supervisor.trace_correlation as mod
        monkeypatch.setattr(mod, "CORRELATION_ENABLED", False)
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": SAMPLE_SPANS}}
        assert correlate_traces(incident, evidence, gateway=None) is None

    def test_empty_chain_no_error_span(self):
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": []}}
        result = correlate_traces(incident, evidence, gateway=None)
        assert result is not None
        assert result["error_span"] is None
        assert result["call_chain"] == []

    def test_gateway_error_falls_back_gracefully(self):
        mock_gw = MagicMock()
        mock_gw.invoke.side_effect = RuntimeError("APM unavailable")
        incident = {"trace_id": TRACE_ID_32}
        result = correlate_traces(incident, {}, gateway=mock_gw)
        # Should still return a result (empty chain), not raise
        assert result is not None
        assert result["call_chain"] == []

    def test_confidence_included_in_result(self):
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": SAMPLE_SPANS}}
        result = correlate_traces(incident, evidence, gateway=None)
        assert 0.0 <= result["correlation_confidence"] <= 1.0

    def test_trace_id_from_log_entry(self):
        incident = {"summary": "slow checkout"}
        evidence = {
            "logs": {
                "logs": [
                    {"_raw": f"ERROR pool.exhausted traceid={TRACE_ID_32} service=payment"}
                ]
            },
            "apm_data": {"spans": SAMPLE_SPANS},
        }
        result = correlate_traces(incident, evidence, gateway=None)
        assert result is not None
        assert result["trace_id"] == TRACE_ID_32


# ---------------------------------------------------------------------------
# Coverage gap-fill tests
# ---------------------------------------------------------------------------

class TestRootServiceEmptyChain:
    """Cover lines 244-245: _build_call_chain sort exception guard + _root_service edge case."""

    def test_root_service_empty_returns_empty_string(self):
        assert _root_service([]) == ""

    def test_correlate_traces_empty_chain_uses_empty_root(self):
        """Ensure correlate_traces populates root_span_service="" for empty chain."""
        incident = {"trace_id": TRACE_ID_32}
        evidence = {"apm_data": {"spans": []}}
        result = correlate_traces(incident, evidence, gateway=None)
        assert result is not None
        assert result["root_span_service"] == ""

    def test_build_call_chain_sort_exception_caught(self):
        """Cover lines 244-245: sort fails when start_times are incomparable types."""
        # Mix a string start_time with an integer start_time → TypeError in sort
        spans = [
            {"service_name": "svc-a", "start_time": "2024-01-01T00:00:00Z"},
            {"service_name": "svc-b", "start_time": 99999},  # int, not comparable with str
        ]
        # Should not raise even though sort fails; chain is returned unsorted
        chain = _build_call_chain(spans)
        assert len(chain) == 2
