"""AG UI Trace Propagation Middleware.

Propagates trace_id across all BFF requests, aligned with OTEL/X-Ray.

Every request to the BFF:
1. Extracts trace_id from header (X-Trace-Id or traceparent)
2. If missing, generates a new trace_id
3. Sets it in request state for downstream use
4. Includes it in all response headers

X-Ray alignment:
  - trace_id format: {version}-{epoch_hex8}-{random_hex24}
  - e.g., 1-67a5a77d-000000001c5f8a2c2b5f89c4
  - AG UI deeplinks: https://console.aws.amazon.com/xray/home#/traces/{trace_id}
"""
from __future__ import annotations

import re
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# X-Ray trace ID format: 1-{epoch_8hex}-{random_24hex}
XRAY_TRACE_ID_PATTERN = re.compile(r"^1-[0-9a-f]{8}-[0-9a-f]{24}$")
# W3C traceparent format: 00-{trace_id_32hex}-{span_id_16hex}-{flags_2hex}
TRACEPARENT_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")

XRAY_CONSOLE_BASE = "https://console.aws.amazon.com/xray/home#/traces/"


def generate_xray_trace_id() -> str:
    """Generate a valid X-Ray trace ID."""
    epoch_hex = format(int(time.time()), "08x")
    random_hex = uuid.uuid4().hex[:24]
    return f"1-{epoch_hex}-{random_hex}"


def extract_trace_id(request: Request) -> str:
    """
    Extract trace_id from request headers.

    Priority:
    1. X-Trace-Id (AG UI custom header)
    2. X-Amzn-Trace-Id (X-Ray passthrough)
    3. traceparent (W3C standard)
    4. Generate new X-Ray trace ID
    """
    # Custom header
    trace_id = request.headers.get("X-Trace-Id", "")
    if trace_id:
        return trace_id

    # X-Ray header
    xray_header = request.headers.get("X-Amzn-Trace-Id", "")
    if xray_header:
        # Parse "Root=1-5e6a28d0-000000001234567890abcdef;Parent=12345678;Sampled=1"
        match = re.search(r"Root=(1-[0-9a-f]{8}-[0-9a-f]{24})", xray_header)
        if match:
            return match.group(1)

    # W3C traceparent
    traceparent = request.headers.get("traceparent", "")
    if traceparent:
        match = TRACEPARENT_PATTERN.match(traceparent)
        if match:
            return match.group(1)

    # Generate new
    return generate_xray_trace_id()


def xray_console_url(trace_id: str) -> str:
    """Build X-Ray console deeplink URL."""
    return f"{XRAY_CONSOLE_BASE}{trace_id}"


class TraceMiddleware(BaseHTTPMiddleware):
    """Injects trace_id into request state and response headers."""

    async def dispatch(self, request: Request, call_next):
        trace_id = extract_trace_id(request)
        request.state.trace_id = trace_id
        request.state.xray_url = xray_console_url(trace_id)

        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-XRay-Url"] = xray_console_url(trace_id)
        return response
