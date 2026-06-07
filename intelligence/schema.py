"""Shared types and deterministic ID generation for the intelligence layer."""

from __future__ import annotations

import hashlib
import math
from enum import Enum


class NodeType(str, Enum):
    METRIC  = "metric"
    LOG     = "log"
    EVENT   = "event"
    CHANGE  = "change"
    TRACE   = "trace"
    ALERT   = "alert"
    CMDB    = "cmdb"
    RUNBOOK = "runbook"
    OUTCOME = "outcome"


class EdgeRelationship(str, Enum):
    CAUSED_BY    = "CAUSED_BY"
    PRECEDED     = "PRECEDED"
    CORRELATED   = "CORRELATED"
    AFFECTS      = "AFFECTS"
    RUNS_ON      = "RUNS_ON"
    HOSTED_ON    = "HOSTED_ON"
    DEPENDS_ON   = "DEPENDS_ON"
    GENERATED_BY = "GENERATED_BY"


class ResolutionStatus(str, Enum):
    SUCCESS         = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED          = "FAILED"


class EntityType(str, Enum):
    SERVICE   = "service"
    HOST      = "host"
    POD       = "pod"
    CONTAINER = "container"
    DATABASE  = "database"
    QUEUE     = "queue"
    ENDPOINT  = "endpoint"
    UNKNOWN   = "unknown"


class InvestigationPhase(str, Enum):
    CREATED     = "created"
    COLLECTING  = "collecting"
    ANALYZING   = "analyzing"
    RESOLVED    = "resolved"
    ARCHIVED    = "archived"


SCHEMA_VERSION = "1.0"

# Source type → NodeType mapping for bridge
SOURCE_NODE_TYPE: dict[str, NodeType] = {
    "logs":            NodeType.LOG,
    "log":             NodeType.LOG,
    "golden_signals":  NodeType.METRIC,
    "metrics":         NodeType.METRIC,
    "apm":             NodeType.TRACE,
    "apm_traces":      NodeType.TRACE,
    "trace":           NodeType.TRACE,
    "k8s_events":      NodeType.EVENT,
    "events":          NodeType.EVENT,
    "changes":         NodeType.CHANGE,
    "change_data":     NodeType.CHANGE,
    "runbook":         NodeType.RUNBOOK,
    "cmdb":            NodeType.CMDB,
    "alerts":          NodeType.ALERT,
    "moogsoft":        NodeType.ALERT,
}


def new_id(*parts: str) -> str:
    """Deterministic 16-char hex ID from parts. sha256[:16]."""
    raw = ":".join(p for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def ts_bucket(timestamp_iso: str, bucket_seconds: int = 10) -> str:
    """Round ISO-8601 timestamp to a bucket to collapse near-simultaneous evidence."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        unix = dt.timestamp()
        bucketed = math.floor(unix / bucket_seconds) * bucket_seconds
        return str(int(bucketed))
    except Exception:
        return timestamp_iso
