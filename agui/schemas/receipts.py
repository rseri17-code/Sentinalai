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
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


CURRENT_RECEIPT_SCHEMA_VERSION = "1.0"


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
            params_redacted=getattr(receipt, "params", {}),
        )


class ReceiptSchema(BaseModel):
    """Schema registry entry for receipt validation."""
    schema_version: str = CURRENT_RECEIPT_SCHEMA_VERSION
    required_fields: list[str] = [
        "receipt_id", "investigation_id", "incident_id",
        "trace_id", "worker", "action",
        "wall_clock_start", "wall_clock_end", "elapsed_ms", "status",
    ]
