"""Durable Workflow Engine — SQLite-backed investigation checkpointing.

Responsibilities:
  start()       — register a new investigation run
  checkpoint()  — persist a phase snapshot after it completes
  resume()      — load the latest checkpoint for an investigation
  complete()    — mark an investigation as successfully finished
  fail()        — mark an investigation as failed

The engine knows nothing about incidents, workers, or LLM calls.
It only tracks execution lifecycle.

SQLite connection pattern follows the existing project convention:
  - WAL journal mode
  - connect-close per operation
  - timeout=10
  - row_factory = sqlite3.Row

Database: WORKFLOW_DB_PATH env var (default: eval/workflow.db)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Optional

from sentinel_core.models.workflow import WorkflowCheckpoint, WorkflowStatus

logger = logging.getLogger("sentinalai.workflow_engine")

_DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "eval", "workflow.db"
)
WORKFLOW_DB_PATH = os.environ.get("WORKFLOW_DB_PATH", _DEFAULT_DB)

# Investigations still RUNNING after this many seconds are considered orphaned
ORPHAN_THRESHOLD_SECONDS = int(os.environ.get("WORKFLOW_ORPHAN_THRESHOLD_SEC", "300"))

# Maximum bytes for evidence snapshot stored in SQLite (truncated if larger)
_MAX_SNAPSHOT_BYTES = int(os.environ.get("WORKFLOW_MAX_SNAPSHOT_BYTES", str(256 * 1024)))


class WorkflowEngine:
    """SQLite-backed durable workflow engine.

    All public methods are thread-safe (each acquires its own connection).
    """

    def __init__(self, db_path: str = WORKFLOW_DB_PATH) -> None:
        self._db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, investigation_id: str, metadata: dict[str, Any] | None = None) -> bool:
        """Register a new investigation run.

        Returns True if the run was created fresh.
        Returns False if the investigation_id already exists (no-op — caller
        should check resume() for existing state).
        """
        now = time.time()
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT status FROM workflow_runs WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if existing:
                logger.debug(
                    "workflow start: %s already exists (status=%s)",
                    investigation_id, existing["status"],
                )
                return False

            conn.execute(
                """INSERT INTO workflow_runs
                   (investigation_id, status, current_phase, completed_phases,
                    started_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    investigation_id,
                    WorkflowStatus.RUNNING.value,
                    "",
                    "[]",
                    now,
                    now,
                    _dumps(metadata or {}),
                ),
            )
            conn.commit()
            logger.info("workflow start: %s", investigation_id)
            return True
        finally:
            conn.close()

    def checkpoint(
        self,
        investigation_id: str,
        phase: str,
        evidence_snapshot: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a phase snapshot after it completes successfully.

        Upserts the checkpoint record and updates the workflow_runs row to
        reflect the new current_phase and the extended completed_phases list.
        """
        now = time.time()
        snap_json = _dumps_capped(evidence_snapshot or {})
        meta_json = _dumps(metadata or {})

        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO workflow_checkpoints
                   (investigation_id, phase, status, evidence_snapshot, metadata,
                    created_at, updated_at)
                   VALUES (?, ?, 'completed', ?, ?, ?, ?)
                   ON CONFLICT(investigation_id, phase) DO UPDATE SET
                       status='completed',
                       evidence_snapshot=excluded.evidence_snapshot,
                       metadata=excluded.metadata,
                       updated_at=excluded.updated_at""",
                (investigation_id, phase, snap_json, meta_json, now, now),
            )

            # Update run's current_phase and extend completed_phases
            run = conn.execute(
                "SELECT completed_phases FROM workflow_runs WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if run:
                existing = json.loads(run["completed_phases"] or "[]")
                if phase not in existing:
                    existing.append(phase)
                conn.execute(
                    """UPDATE workflow_runs
                       SET current_phase=?, completed_phases=?, updated_at=?
                       WHERE investigation_id=?""",
                    (_dumps(existing), json.dumps(existing), now, investigation_id),
                )
                conn.execute(
                    """UPDATE workflow_runs
                       SET current_phase=?, completed_phases=?, updated_at=?
                       WHERE investigation_id=?""",
                    (phase, json.dumps(existing), now, investigation_id),
                )
            conn.commit()
            logger.debug("workflow checkpoint: %s phase=%s", investigation_id, phase)
        finally:
            conn.close()

    def resume(self, investigation_id: str) -> Optional[WorkflowCheckpoint]:
        """Load the latest checkpoint for an investigation.

        Returns None if no run or checkpoint exists.
        """
        conn = self._connect()
        try:
            run = conn.execute(
                "SELECT * FROM workflow_runs WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if not run:
                return None

            # Latest checkpoint for this run
            cp = conn.execute(
                """SELECT * FROM workflow_checkpoints
                   WHERE investigation_id = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (investigation_id,),
            ).fetchone()

            completed = json.loads(run["completed_phases"] or "[]")
            evidence = _loads(cp["evidence_snapshot"] if cp else None)
            meta = _loads(run["metadata"])

            return WorkflowCheckpoint(
                investigation_id=investigation_id,
                phase=cp["phase"] if cp else run["current_phase"] or "",
                status=WorkflowStatus(run["status"]),
                completed_phases=completed,
                evidence_snapshot=evidence,
                result_snapshot=_loads(run["result_summary"]),
                created_at=run["started_at"],
                updated_at=run["updated_at"],
                error=run["error"] or "",
                metadata=meta,
            )
        finally:
            conn.close()

    def complete(
        self,
        investigation_id: str,
        result_summary: dict[str, Any] | None = None,
    ) -> None:
        """Mark an investigation as successfully completed."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE workflow_runs
                   SET status=?, completed_at=?, updated_at=?, result_summary=?
                   WHERE investigation_id=?""",
                (
                    WorkflowStatus.COMPLETED.value,
                    now,
                    now,
                    _dumps_capped(result_summary or {}),
                    investigation_id,
                ),
            )
            conn.commit()
            logger.info("workflow complete: %s", investigation_id)
        finally:
            conn.close()

    def fail(self, investigation_id: str, error: str = "") -> None:
        """Mark an investigation as failed."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE workflow_runs
                   SET status=?, updated_at=?, error=?
                   WHERE investigation_id=?""",
                (WorkflowStatus.FAILED.value, now, error[:2000], investigation_id),
            )
            conn.commit()
            logger.info("workflow fail: %s error=%s", investigation_id, error[:120])
        finally:
            conn.close()

    def get_status(self, investigation_id: str) -> Optional[WorkflowStatus]:
        """Return current status for an investigation, or None if unknown."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM workflow_runs WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            return WorkflowStatus(row["status"]) if row else None
        finally:
            conn.close()

    def get_timeline(self, investigation_id: str) -> list[dict[str, Any]]:
        """Return structured phase timeline for UI display.

        Returns list of phase dicts ordered by time:
            [{phase, status, started_at, completed_at, duration_ms, metadata}, ...]
        plus a synthetic "run" entry for the overall investigation.
        """
        conn = self._connect()
        try:
            run = conn.execute(
                "SELECT * FROM workflow_runs WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if not run:
                return []

            checkpoints = conn.execute(
                """SELECT * FROM workflow_checkpoints
                   WHERE investigation_id = ?
                   ORDER BY updated_at ASC""",
                (investigation_id,),
            ).fetchall()

            timeline: list[dict[str, Any]] = [
                {
                    "phase": "investigation",
                    "status": run["status"],
                    "started_at": run["started_at"],
                    "completed_at": run["completed_at"],
                    "duration_ms": (
                        (run["completed_at"] - run["started_at"]) * 1000
                        if run["completed_at"] else None
                    ),
                    "error": run["error"] or "",
                    "metadata": _loads(run["metadata"]),
                }
            ]
            for cp in checkpoints:
                duration = (cp["updated_at"] - cp["created_at"]) * 1000
                timeline.append({
                    "phase": cp["phase"],
                    "status": cp["status"],
                    "started_at": cp["created_at"],
                    "completed_at": cp["updated_at"],
                    "duration_ms": duration,
                    "error": "",
                    "metadata": _loads(cp["metadata"]),
                })
            return timeline
        finally:
            conn.close()

    def find_orphaned(
        self,
        max_age_seconds: int = ORPHAN_THRESHOLD_SECONDS,
    ) -> list[str]:
        """Return investigation_ids that are RUNNING but stale (likely crashed)."""
        cutoff = time.time() - max_age_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT investigation_id FROM workflow_runs
                   WHERE status = ? AND updated_at < ?""",
                (WorkflowStatus.RUNNING.value, cutoff),
            ).fetchall()
            return [r["investigation_id"] for r in rows]
        finally:
            conn.close()

    def purge_old(self, max_age_seconds: int = 86400 * 7) -> int:
        """Delete completed/failed runs older than max_age_seconds. Returns count."""
        cutoff = time.time() - max_age_seconds
        conn = self._connect()
        try:
            ids = conn.execute(
                """SELECT investigation_id FROM workflow_runs
                   WHERE status IN (?, ?) AND updated_at < ?""",
                (WorkflowStatus.COMPLETED.value, WorkflowStatus.FAILED.value, cutoff),
            ).fetchall()
            if not ids:
                return 0
            id_list = [r["investigation_id"] for r in ids]
            placeholders = ",".join("?" * len(id_list))
            conn.execute(
                f"DELETE FROM workflow_checkpoints WHERE investigation_id IN ({placeholders})",
                id_list,
            )
            conn.execute(
                f"DELETE FROM workflow_runs WHERE investigation_id IN ({placeholders})",
                id_list,
            )
            conn.commit()
            return len(id_list)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    investigation_id TEXT PRIMARY KEY,
                    status           TEXT NOT NULL DEFAULT 'running',
                    current_phase    TEXT,
                    completed_phases TEXT NOT NULL DEFAULT '[]',
                    started_at       REAL NOT NULL,
                    updated_at       REAL NOT NULL,
                    completed_at     REAL,
                    error            TEXT,
                    result_summary   TEXT,
                    metadata         TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wf_status ON workflow_runs (status, updated_at)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    investigation_id TEXT NOT NULL,
                    phase            TEXT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'completed',
                    evidence_snapshot TEXT,
                    metadata         TEXT,
                    created_at       REAL NOT NULL,
                    updated_at       REAL NOT NULL,
                    PRIMARY KEY (investigation_id, phase)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cp_inv ON workflow_checkpoints (investigation_id, updated_at)"
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_engine: WorkflowEngine | None = None
_engine_lock = __import__("threading").Lock()


def get_engine(db_path: str = WORKFLOW_DB_PATH) -> WorkflowEngine:
    """Return the module-level WorkflowEngine singleton."""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            _engine = WorkflowEngine(db_path)
    return _engine


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return "{}"


def _dumps_capped(obj: Any) -> str:
    """Serialize to JSON, truncating if over the size cap."""
    raw = _dumps(obj)
    if len(raw) > _MAX_SNAPSHOT_BYTES:
        # Store a size-safe summary instead of the full snapshot
        return json.dumps({"_truncated": True, "_original_bytes": len(raw)})
    return raw


def _loads(text: Any) -> dict[str, Any]:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}
