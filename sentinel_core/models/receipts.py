"""AG UI Receipt Schema v1.0

Receipts are immutable evidence records. Every tool call MUST produce a receipt.
No claim without evidence. No evidence without receipt.

Extends the existing supervisor/receipt.py with:
- receipt_id (UUID, primary key)
- schema_version
- investigation_id
- sequence_num (ordering within investigation)
- evidence_refs (cross-links to other receipts/events)
- output_summary (safe, non-sensitive summary)
- storage_uri (S3 path for full receipt)
- hash (SHA256 of canonical payload for integrity)
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


CURRENT_RECEIPT_SCHEMA_VERSION = "1.0"


# Sensitive key-name substrings (case-insensitive) that always redact the value.
# Ordered for deterministic doc-listing only; membership test is a set.
_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "bearer", "credential", "private_key",
    "privatekey", "session", "cookie", "ssn", "dob",
)

# Value patterns that look like a secret regardless of key name.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),          # OpenAI-style / generic sk-
    re.compile(r"AKIA[0-9A-Z]{16}"),                 # AWS access key
    re.compile(r"aws_(?:secret|access)_?key", re.I), # named AWS secret markers
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}"),  # Bearer <jwt>
    re.compile(r"ey[JI][A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),             # GitHub PAT
    re.compile(r"xox[bpoas]-[A-Za-z0-9\-]{10,}"),   # Slack tokens
)

_REDACTED_PLACEHOLDER = "***REDACTED***"


def _redact_value(value: Any) -> Any:
    """Return the value unchanged, or the redaction placeholder if it looks
    like a secret. Recurses one level into dicts/lists so a nested credentials
    map does not escape.

    Deterministic: same input → same output. No state, no I/O.
    """
    if isinstance(value, str):
        for pat in _SECRET_VALUE_PATTERNS:
            if pat.search(value):
                return _REDACTED_PLACEHOLDER
        return value
    if isinstance(value, dict):
        return _redact_params(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_params(params: Any) -> dict[str, Any]:
    """Return a redacted copy of a params dict.

    A key is redacted whole (value replaced with the placeholder) when its
    lowercased name contains any sensitive substring. Otherwise the value
    is passed through :func:`_redact_value` which pattern-matches string
    values against known secret shapes.

    Non-dict inputs return an empty dict (defensive: the receipt field's
    contract is dict[str, Any]).
    """
    if not isinstance(params, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        k_str = str(k)
        if any(s in k_str.lower() for s in _SENSITIVE_KEY_SUBSTRINGS):
            out[k_str] = _REDACTED_PLACEHOLDER
        else:
            out[k_str] = _redact_value(v)
    return out


class UIReceipt(BaseModel):
    """Immutable receipt — once written, never mutated."""
    model_config = {"extra": "allow"}

    # Identity
    receipt_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = Field(default=CURRENT_RECEIPT_SCHEMA_VERSION)

    # Context
    investigation_id: str
    incident_id: str
    sequence_num: int

    # Provenance (OTEL alignment)
    trace_id: str
    span_id: str = Field(default="")
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])

    # Tool call identity
    worker: str          # e.g., "LogWorker"
    tool: str            # e.g., "Splunk"
    action: str          # e.g., "search_logs"
    mcp_target: str = Field(default="")  # e.g., "SplunkTarget"

    # Timing
    wall_clock_start: str  # ISO 8601
    wall_clock_end: str    # ISO 8601
    elapsed_ms: float

    # Status
    status: str  # pending | success | error | timeout
    error: Optional[str] = None
    result_count: int = 0

    # Evidence (redacted — no secrets)
    params_redacted: dict[str, Any] = Field(default_factory=dict)
    output_summary: Optional[str] = None  # Brief human-readable summary

    # Cross-references
    evidence_refs: list[str] = Field(default_factory=list)  # receipt_ids this depends on

    # Storage
    storage_uri: Optional[str] = None  # s3://bucket/path/receipt.json

    # Integrity
    payload_hash: str = Field(default="")  # SHA256 of canonical payload

    def compute_hash(self) -> str:
        """Compute SHA256 of canonical (deterministic) payload."""
        canonical = {
            "investigation_id": self.investigation_id,
            "incident_id": self.incident_id,
            "sequence_num": self.sequence_num,
            "worker": self.worker,
            "action": self.action,
            "wall_clock_start": self.wall_clock_start,
            "wall_clock_end": self.wall_clock_end,
            "elapsed_ms": self.elapsed_ms,
            "status": self.status,
        }
        raw = json.dumps(canonical, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def model_post_init(self, __context: Any) -> None:
        if not self.payload_hash:
            self.payload_hash = self.compute_hash()

    def to_dynamo(self) -> dict[str, Any]:
        data = self.model_dump()
        data["pk"] = f"INVESTIGATION#{self.investigation_id}"
        data["sk"] = f"RECEIPT#{self.sequence_num:08d}#{self.receipt_id}"
        data["gsi1pk"] = f"RECEIPT#{self.receipt_id}"
        data["gsi1sk"] = self.wall_clock_start
        return data

    @classmethod
    def from_supervisor_receipt(
        cls,
        receipt: Any,  # supervisor.receipt.Receipt
        investigation_id: str,
        incident_id: str,
        sequence_num: int,
        worker: str,
    ) -> "UIReceipt":
        """Bridge from existing supervisor Receipt to UIReceipt."""
        return cls(
            investigation_id=investigation_id,
            incident_id=incident_id,
            sequence_num=sequence_num,
            trace_id=getattr(receipt, "trace_id", ""),
            correlation_id=getattr(receipt, "correlation_id", ""),
            worker=worker,
            tool=getattr(receipt, "tool", worker),
            action=getattr(receipt, "action", "unknown"),
            wall_clock_start=getattr(receipt, "wall_clock_start", ""),
            wall_clock_end=getattr(receipt, "wall_clock_end", ""),
            elapsed_ms=float(getattr(receipt, "elapsed_ms", 0)),
            status=getattr(receipt, "status", "unknown"),
            error=getattr(receipt, "error", None),
            result_count=getattr(receipt, "result_count", 0),
            params_redacted=_redact_params(getattr(receipt, "params", {})),
        )


class ReceiptSchema(BaseModel):
    """Schema registry entry for receipt validation."""
    schema_version: str = CURRENT_RECEIPT_SCHEMA_VERSION
    required_fields: list[str] = [
        "receipt_id", "investigation_id", "incident_id",
        "trace_id", "worker", "action",
        "wall_clock_start", "wall_clock_end", "elapsed_ms", "status",
    ]
