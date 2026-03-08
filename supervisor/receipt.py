"""Evidence receipt model for SentinalAI.

Every worker call produces a Receipt that tracks:
- tool/action invoked, with parameters
- timing (start, end, elapsed) — both monotonic and wall-clock
- result summary (count, status, error)
- correlation ID for tracing
- policy decision reference (which policy authorized this call)
- OTEL trace ID linkage for cross-correlation
- optional full output capture (gated by RECEIPT_CAPTURE_OUTPUT)
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# G5.1: Gate full output capture behind env var
RECEIPT_CAPTURE_OUTPUT = os.environ.get("RECEIPT_CAPTURE_OUTPUT", "").lower() in ("1", "true", "yes")


@dataclass
class Receipt:
    """Immutable evidence receipt for a single worker call."""

    tool: str
    action: str
    params: dict = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    start_ts: float = 0.0
    end_ts: float = 0.0
    elapsed_ms: float = 0.0
    status: str = "pending"  # pending | success | error | timeout
    result_count: int = 0
    error: str = ""
    case_id: str = ""
    # G5.2: Policy decision reference — records which rule authorized the call
    policy_ref: str = ""
    # G5.3: Wall-clock timestamps (ISO 8601) for cross-system correlation
    wall_clock_start: str = ""
    wall_clock_end: str = ""
    # G5.4: OTEL trace ID linkage for cross-correlation with distributed traces
    trace_id: str = ""
    # G5.1: Full output capture (when RECEIPT_CAPTURE_OUTPUT is enabled)
    output: dict | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence / replay."""
        d = asdict(self)
        # Omit output field if not captured to keep payloads small
        if d.get("output") is None:
            d.pop("output", None)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Receipt:
        """Reconstruct from a persisted dict."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _count_results(result: dict | None) -> int:
    """Heuristically count result items for receipt metadata."""
    if not result or not isinstance(result, dict):
        return 0
    # Check common patterns
    for key in ("results", "events", "changes", "metrics", "similar_incidents"):
        val = result.get(key)
        if isinstance(val, list):
            return len(val)
        if isinstance(val, dict):
            inner = val.get("results") or val.get("metrics")
            if isinstance(inner, list):
                return len(inner)
    # Has an incident?
    if "incident" in result:
        return 1
    return 0


class ReceiptCollector:
    """Collects receipts for a single investigation case."""

    def __init__(self, case_id: str = "", trace_id: str = ""):
        self.case_id = case_id
        # G5.4: Store the OTEL trace ID for the investigation
        self.trace_id = trace_id
        self.receipts: list[Receipt] = []

    def start(
        self,
        tool: str,
        action: str,
        params: dict,
        policy_ref: str = "",
    ) -> Receipt:
        """Create and register a new receipt (call this before the worker call)."""
        receipt = Receipt(
            tool=tool,
            action=action,
            params=_redact_params(params),
            case_id=self.case_id,
            start_ts=time.monotonic(),
            wall_clock_start=datetime.now(timezone.utc).isoformat(),
            policy_ref=policy_ref,
            trace_id=self.trace_id,
        )
        self.receipts.append(receipt)
        return receipt

    def finish(self, receipt: Receipt, result: dict | None, error: str = "") -> None:
        """Finalize a receipt after the worker call completes."""
        receipt.end_ts = time.monotonic()
        receipt.elapsed_ms = round((receipt.end_ts - receipt.start_ts) * 1000, 1)
        receipt.wall_clock_end = datetime.now(timezone.utc).isoformat()
        if error:
            receipt.status = "error"
            receipt.error = error
        else:
            receipt.status = "success"
            receipt.result_count = _count_results(result)
            # G5.1: Capture full output when enabled
            if RECEIPT_CAPTURE_OUTPUT and result is not None:
                receipt.output = _redact_output(result)

    def to_list(self) -> list[dict]:
        """Serialize all receipts."""
        return [r.to_dict() for r in self.receipts]

    def summary(self) -> dict:
        """Return a summary of all receipts for this case."""
        total = len(self.receipts)
        succeeded = sum(1 for r in self.receipts if r.status == "success")
        failed = sum(1 for r in self.receipts if r.status == "error")
        total_ms = sum(r.elapsed_ms for r in self.receipts)
        return {
            "case_id": self.case_id,
            "total_calls": total,
            "succeeded": succeeded,
            "failed": failed,
            "total_elapsed_ms": round(total_ms, 1),
        }


def _redact_params(params: dict) -> dict:
    """Redact sensitive fields from params before storing in receipt."""
    redact_keys = {"password", "token", "secret", "api_key", "authorization"}
    return {
        k: "***REDACTED***" if k.lower() in redact_keys else v
        for k, v in params.items()
    }


def _redact_output(result: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive fields from output before storing in receipt."""
    if not isinstance(result, dict):
        return result
    redact_keys = {"password", "token", "secret", "api_key", "authorization", "credentials"}
    redacted: dict[str, Any] = {}
    for k, v in result.items():
        if k.lower() in redact_keys:
            redacted[k] = "***REDACTED***"
        elif isinstance(v, dict):
            redacted[k] = _redact_output(v)
        else:
            redacted[k] = v
    return redacted
