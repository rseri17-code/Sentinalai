"""Distributed trace correlation for SentinalAI.

Extracts trace IDs from incident alerts and APM evidence, then builds a
causal chain across services so the investigation has full cross-service
context rather than just single-service symptoms.

Problem:
    An alert fires on payment-service with latency p99 > 5s.
    The root cause is actually in auth-service (session lookup timeout)
    which propagates upstream.  Single-service evidence misses the true cause.

Solution:
    1. Extract trace_id from the incident alert / ITSM / APM data.
    2. Query APM (Dynatrace) for all spans in that trace.
    3. Build a service call chain: client → auth-service → payment-service → db.
    4. Identify the slowest / erroring span.
    5. Attach the chain to evidence as "trace_correlation".

The investigation engine then has cross-service context and can correctly
attribute root cause to the upstream failure.

Output added to evidence["trace_correlation"]:
    {
        "trace_id": "abc123...",
        "root_span_service": "auth-service",
        "error_span": {"service": "auth-service", "operation": "session_lookup",
                       "duration_ms": 4820, "error": "connection_timeout"},
        "call_chain": [
            {"service": "client",          "operation": "checkout",         "duration_ms": 5200},
            {"service": "payment-service", "operation": "process_payment",  "duration_ms": 5180},
            {"service": "auth-service",    "operation": "session_lookup",   "duration_ms": 4820, "error": "connection_timeout"},
            {"service": "session-db",      "operation": "redis_get",        "duration_ms": 4810},
        ],
        "cross_service_impact": ["payment-service", "checkout-service"],
        "correlation_confidence": 0.92,
    }

Configuration:
    TRACE_CORRELATION_ENABLED — on/off (default: true)
    TRACE_ID_FIELDS           — comma-separated field names to check for trace IDs
                                (default: "trace_id,traceId,x-trace-id,traceid")
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger("sentinalai.trace_correlation")

CORRELATION_ENABLED = os.environ.get(
    "TRACE_CORRELATION_ENABLED", "true"
).lower() in ("1", "true", "yes")

_TRACE_ID_FIELDS = [
    f.strip()
    for f in os.environ.get(
        "TRACE_ID_FIELDS",
        "trace_id,traceId,x-trace-id,traceid,x_b3_traceid,dd-trace-id",
    ).split(",")
]

# Regex: hexadecimal trace ID (16 or 32 chars) or UUID format
_TRACE_RE = re.compile(
    r"\b([0-9a-f]{32}|[0-9a-f]{16}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def correlate_traces(
    incident: dict[str, Any],
    evidence: dict[str, Any],
    gateway: Any | None = None,
) -> dict[str, Any] | None:
    """Extract trace ID and build cross-service call chain.

    Args:
        incident:  The raw incident dict (summary, metadata, etc.)
        evidence:  Evidence dict (may already contain apm_data)
        gateway:   McpGateway instance for APM queries (optional)

    Returns:
        Correlation dict ready for evidence["trace_correlation"], or None
        if no trace ID found or correlation is disabled.
    """
    if not CORRELATION_ENABLED:
        return None

    # Step 1: Find a trace ID
    trace_id = _extract_trace_id(incident, evidence)
    if not trace_id:
        logger.debug("No trace ID found in incident/evidence — skipping correlation")
        return None

    logger.info("Trace correlation: found trace_id=%s", trace_id[:16])

    # Step 2: Fetch full trace from APM if gateway is available
    raw_trace = _fetch_trace(trace_id, evidence, gateway)

    # Step 3: Build call chain from the trace data
    call_chain = _build_call_chain(raw_trace)

    # Step 4: Identify error span and root service
    error_span = _find_error_span(call_chain)
    root_span_service = error_span.get("service", "") if error_span else _root_service(call_chain)

    # Step 5: Compute cross-service impact
    cross_service = list({s["service"] for s in call_chain if s.get("service")})

    # Step 6: Confidence based on chain completeness
    confidence = _correlation_confidence(trace_id, call_chain, error_span)

    result = {
        "trace_id": trace_id,
        "root_span_service": root_span_service,
        "error_span": error_span,
        "call_chain": call_chain,
        "cross_service_impact": cross_service,
        "correlation_confidence": confidence,
        "chain_depth": len(call_chain),
    }
    logger.info(
        "Trace correlation complete: chain_depth=%d root=%s confidence=%.2f",
        len(call_chain), root_span_service, confidence,
    )
    return result


def extract_trace_id_from_text(text: str) -> str | None:
    """Extract first trace ID found in arbitrary text."""
    m = _TRACE_RE.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_trace_id(incident: dict, evidence: dict) -> str | None:
    """Search for a trace ID across incident fields and evidence."""
    # 1. Direct fields on incident
    for field in _TRACE_ID_FIELDS:
        val = incident.get(field, "")
        if val and isinstance(val, str):
            return val.strip()

    # 2. Nested in incident metadata
    meta = incident.get("metadata") or incident.get("properties") or {}
    if isinstance(meta, dict):
        for field in _TRACE_ID_FIELDS:
            val = meta.get(field, "")
            if val:
                return str(val).strip()

    # 3. APM data already in evidence
    apm = evidence.get("apm_data") or evidence.get("apm") or {}
    if isinstance(apm, dict):
        for field in _TRACE_ID_FIELDS:
            val = apm.get(field, "")
            if val:
                return str(val).strip()
        # APM error samples often embed trace IDs
        _err_samples: list = apm.get("errors") or apm.get("error_samples") or []
        for err in _err_samples[:5]:
            if isinstance(err, dict):
                for field in _TRACE_ID_FIELDS:
                    val = err.get(field, "")
                    if val:
                        return str(val).strip()

    # 4. Extract from incident summary text
    summary = incident.get("summary", "") or incident.get("description", "")
    if summary:
        found = extract_trace_id_from_text(summary)
        if found:
            return found

    # 5. Extract from log entries
    logs_block = evidence.get("logs") or evidence.get("log_data") or {}
    if isinstance(logs_block, dict):
        for entry in (logs_block.get("logs") or logs_block.get("results") or [])[:10]:
            raw = entry.get("_raw") or entry.get("message") or ""
            found = extract_trace_id_from_text(str(raw))
            if found:
                return found

    return None


def _fetch_trace(
    trace_id: str,
    evidence: dict,
    gateway: Any | None,
) -> list[dict]:
    """Fetch trace spans from APM. Falls back to evidence if gateway unavailable."""
    # Use already-collected APM data first
    apm = evidence.get("apm_data") or evidence.get("apm") or {}
    if isinstance(apm, dict):
        spans = apm.get("spans") or apm.get("trace_spans") or []
        if spans:
            return spans

    # Try gateway if available
    if gateway is not None:
        try:
            result = gateway.invoke(
                "dynatrace.get_trace",
                "get_trace",
                {"trace_id": trace_id},
            )
            if isinstance(result, dict) and not result.get("error"):
                return result.get("spans", [])
        except Exception as exc:
            logger.debug("Failed to fetch trace from APM: %s", exc)

    return []


def _build_call_chain(spans: list[dict]) -> list[dict]:
    """Build a sorted call chain from raw APM spans.

    Sorts by start time (ascending) so the chain reads root → leaf.
    Each entry: {service, operation, duration_ms, error, span_id, parent_id}
    """
    if not spans:
        return []

    chain = []
    for span in spans:
        chain.append({
            "service":     span.get("service_name") or span.get("service") or "",
            "operation":   span.get("operation_name") or span.get("operation") or "",
            "duration_ms": float(span.get("duration_ms") or span.get("duration") or 0),
            "error":       span.get("error") or span.get("error_message") or "",
            "span_id":     span.get("span_id") or "",
            "parent_id":   span.get("parent_span_id") or span.get("parent_id") or "",
            "start_time":  span.get("start_time") or span.get("timestamp") or "",
        })

    # Sort by start_time (if available), then by duration descending
    try:
        chain.sort(key=lambda s: s.get("start_time", "") or "")
    except Exception:
        pass

    return chain


def _find_error_span(chain: list[dict]) -> dict | None:
    """Return the span with an error (slowest error wins if multiple)."""
    error_spans = [s for s in chain if s.get("error")]
    if not error_spans:
        return None
    return max(error_spans, key=lambda s: s.get("duration_ms", 0))


def _root_service(chain: list[dict]) -> str:
    """Return the service at the top of the chain (root caller)."""
    return chain[0].get("service", "") if chain else ""


def _correlation_confidence(
    trace_id: str,
    chain: list[dict],
    error_span: dict | None,
) -> float:
    """Score confidence in the correlation result."""
    if not trace_id:
        return 0.0
    score = 0.5

    # Populated chain
    if len(chain) >= 2:
        score += 0.2
    if len(chain) >= 4:
        score += 0.1

    # Found an error span
    if error_span:
        score += 0.2

    return round(min(score, 1.0), 2)
