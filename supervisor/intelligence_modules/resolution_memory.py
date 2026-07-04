"""ResolutionMemory runner for the Intelligence Runtime.

Wires the existing ``intelligence.resolution_memory`` module into the
Phase 19 Intelligence Runtime at ``POST_PERSIST``. No changes to
ResolutionMemory itself — the runner is a thin adapter that:

1. Reads investigation context from ``RuntimeContext``.
2. Deduplicates by ``investigation_id`` (bypassing the timestamped
   ``memory_id`` PK limitation of the underlying store without
   modifying ResolutionMemory).
3. Delegates persistence to ``ResolutionMemoryStore.record()`` verbatim.

Feature-flag-gated: registered under ``ENABLE_RESOLUTION_MEMORY_WRITE``.
When the flag is off, the module is skipped by the runtime (returns
status=skipped without invoking this runner).

Never raises. Any failure inside the runner is caught by the runtime's
failure isolation and reported on the ModuleResult.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.resolution_memory")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESOLUTION_MEMORY_FEATURE_FLAG = "ENABLE_RESOLUTION_MEMORY_WRITE"
WRITE_VERSION = 1

# Root-cause prefixes that indicate an investigation did not produce an
# actionable outcome. Recording these as memory is noise; skip them.
_SKIP_PREFIXES = ("INSUFFICIENT", "META_QUERY", "BLOCKED", "LOW CONFIDENCE")


# ---------------------------------------------------------------------------
# ModuleSpec — declarative registration
# ---------------------------------------------------------------------------

RESOLUTION_MEMORY_SPEC = ModuleSpec(
    name="resolution_memory",
    stage=IntelligenceStage.POST_PERSIST,
    feature_flag=RESOLUTION_MEMORY_FEATURE_FLAG,
    priority=100,
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def resolution_memory_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Persist a candidate ResolutionMemory for a completed investigation.

    Runs at POST_PERSIST — after the investigation is complete, winner
    selected, confidence finalized, receipts finalized. Never before.

    Returns a metadata dict that the runtime lifts onto
    ``receipt.metadata["intelligence"]`` under this module's entry:

        {status, record_id, deduplicated, write_time, confidence, version}

    Statuses:
        success       — new record written
        deduplicated  — existing record found for this investigation_id
        skipped       — non-actionable root cause; nothing written
        failed        — internal error (runtime captures error_type)
    """
    result = ctx.result or {}
    root_cause = str(result.get("root_cause", "") or "").strip()
    if not root_cause or root_cause.startswith(_SKIP_PREFIXES):
        return {
            "status":  "skipped",
            "reason":  "no_actionable_root_cause",
            "version": WRITE_VERSION,
        }

    fetch_out = ctx.fetch_out or {}
    incident = fetch_out.get("incident") if isinstance(fetch_out, dict) else None
    incident_id = ""
    if isinstance(incident, dict):
        incident_id = str(incident.get("incident_id") or "")
    if not incident_id:
        incident_id = ctx.investigation_id  # last-resort fallback

    service = ""
    if isinstance(fetch_out, dict):
        service = str(fetch_out.get("service", "") or "")

    incident_type = ""
    if ctx.cres is not None:
        incident_type = str(getattr(ctx.cres, "incident_type", "") or "")

    evidence: dict[str, Any] = {}
    if ctx.aout is not None:
        _ev = getattr(ctx.aout, "evidence", None)
        if isinstance(_ev, dict):
            evidence = _ev

    db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")

    # Ensure the resolution_memories table exists at THIS db_path. The
    # ops_persistence singleton captures OPS_DB_PATH at module-import time,
    # so it cannot be trusted to have run its DDL against the current
    # env-var path. Idempotent — CREATE TABLE IF NOT EXISTS is a no-op when
    # the table already exists.
    _ensure_schema(db_path)

    from intelligence.resolution_memory import (
        ResolutionMemory,
        ResolutionMemoryStore,
    )

    store = ResolutionMemoryStore(db_path)

    # Deduplication — query for a pre-existing record with this
    # investigation_id. The underlying store keys by memory_id which
    # includes recorded_at[:19]; two calls at different times would
    # produce different IDs. Guard against that by checking here.
    existing = _find_by_investigation_id(
        store,
        investigation_id=ctx.investigation_id,
        service=service,
        incident_type=incident_type,
    )
    if existing is not None:
        return {
            "status":        "deduplicated",
            "record_id":     existing.memory_id,
            "deduplicated":  True,
            "write_time":    existing.recorded_at,
            "confidence":    existing.confidence,
            "version":       WRITE_VERSION,
        }

    memory = ResolutionMemory.from_investigation(
        investigation_id=ctx.investigation_id,
        incident_id=incident_id,
        service=service,
        incident_type=incident_type,
        result=result,
        evidence=evidence,
    )
    store.record(memory)

    return {
        "status":        "success",
        "record_id":     memory.memory_id,
        "deduplicated":  False,
        "write_time":    memory.recorded_at,
        "confidence":    memory.confidence,
        "version":       WRITE_VERSION,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# SQLite DDL, copied verbatim from database/ops_persistence.py so this
# module can operate independently of the ops_persistence singleton (which
# captures OPS_DB_PATH at module-import time and is therefore untrusted in
# env-var-tuned test environments and multi-DB deployments).
_RESOLUTION_MEMORIES_DDL = """
    CREATE TABLE IF NOT EXISTS resolution_memories (
        memory_id            TEXT PRIMARY KEY,
        investigation_id     TEXT NOT NULL DEFAULT '',
        incident_id          TEXT NOT NULL DEFAULT '',
        service              TEXT NOT NULL DEFAULT '',
        environment          TEXT NOT NULL DEFAULT '',
        incident_type        TEXT NOT NULL DEFAULT '',
        symptoms             TEXT NOT NULL DEFAULT '[]',
        detected_root_cause  TEXT NOT NULL DEFAULT '',
        evidence_used        TEXT NOT NULL DEFAULT '[]',
        confirmed_resolution TEXT NOT NULL DEFAULT '',
        fix_action           TEXT NOT NULL DEFAULT '',
        rollback_action      TEXT NOT NULL DEFAULT '',
        owner_team           TEXT NOT NULL DEFAULT '',
        confidence           INTEGER NOT NULL DEFAULT 0,
        validation_status    TEXT NOT NULL DEFAULT 'candidate',
        is_confirmed         INTEGER NOT NULL DEFAULT 0,
        lesson_learned       TEXT NOT NULL DEFAULT '',
        related_incident_ids TEXT NOT NULL DEFAULT '[]',
        mttr_minutes         REAL NOT NULL DEFAULT 0,
        recorded_at          TEXT NOT NULL DEFAULT '',
        confirmed_at         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rm_service ON resolution_memories(service);
    CREATE INDEX IF NOT EXISTS idx_rm_type ON resolution_memories(incident_type);
    CREATE INDEX IF NOT EXISTS idx_rm_status ON resolution_memories(validation_status);
    CREATE INDEX IF NOT EXISTS idx_rm_recorded ON resolution_memories(recorded_at DESC);
"""


def _ensure_schema(db_path: str) -> None:
    """Ensure resolution_memories table + indexes exist at db_path.

    Idempotent, non-raising. Creates the parent directory if missing.
    """
    try:
        import sqlite3
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_RESOLUTION_MEMORIES_DDL)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("resolution_memory: schema ensure failed: %s", exc)


def _find_by_investigation_id(
    store: Any,
    *,
    investigation_id: str,
    service: str = "",
    incident_type: str = "",
) -> Optional[Any]:
    """Return the first ResolutionMemory whose ``investigation_id`` matches.

    Uses ``store.query()`` with the (service, incident_type) indexed
    filter to keep the scan bounded, then filters in Python. Returns
    ``None`` on no match or on query failure. Never raises.
    """
    try:
        candidates = store.query(
            service=service or None,
            incident_type=incident_type or None,
            limit=200,
        )
    except Exception as exc:
        logger.debug("resolution_memory: dedup query failed: %s", exc)
        return None
    for m in candidates:
        if m.investigation_id == investigation_id:
            return m
    return None


__all__ = [
    "RESOLUTION_MEMORY_SPEC",
    "RESOLUTION_MEMORY_FEATURE_FLAG",
    "WRITE_VERSION",
    "resolution_memory_runner",
]
