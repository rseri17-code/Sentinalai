"""Operational intelligence persistence — SQLite-backed durable state store.

Persists the nine intelligence state objects that were previously in-memory:
  1. EnrichedToolReceipt        (tool call transparency records)
  2. Adaptive intelligence       (threshold + strategy state — event log)
  3. Source weight history       (EMA weight evolution per step)
  4. Pattern reinforcement       (pattern prediction outcomes)
  5. Confidence convergence      (ECE snapshots over time)
  6. Feedback outcome history    (investigation quality over time)
  7. Replay validation metadata  (HarnessReflection per investigation)
  8. Learning safety events      (drift, circuit breaker, stale calibration)
  9. Intelligence warming state  (key-value flags)

Design decisions:
  - SQLite in WAL mode: single file, ACID, no server, survives restart
  - Async write queue: caller never blocks; background thread batches writes
  - Corruption guard: PRAGMA integrity_check on startup; rename+recreate on fail
  - Bounded retention: DELETE WHERE timestamp < cutoff run on startup
  - Forward-only migration: schema_version tracked; _MIGRATIONS applied at startup
  - All reads wrapped in try/except; DB unavailability never blocks RCA pipeline
  - Queue saturation tracked: dropped writes and saturation events counted
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.ops_persistence")

# ── Config ────────────────────────────────────────────────────────────────────

OPS_DB_PATH = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
OPS_DB_ENABLED = os.environ.get("OPS_DB_ENABLED", "true").lower() not in ("0", "false", "no")

# Retention windows
_RETENTION_RECEIPTS_DAYS = int(os.environ.get("OPS_RETENTION_RECEIPTS_DAYS", "30"))
_RETENTION_HISTORY_DAYS  = int(os.environ.get("OPS_RETENTION_HISTORY_DAYS",  "90"))
_RETENTION_SAFETY_DAYS   = int(os.environ.get("OPS_RETENTION_SAFETY_DAYS",   "30"))

# Async write queue
_QUEUE_MAX   = int(os.environ.get("OPS_QUEUE_MAX",   "2000"))
_BATCH_SIZE  = int(os.environ.get("OPS_BATCH_SIZE",  "50"))
_BATCH_DELAY = float(os.environ.get("OPS_BATCH_DELAY", "1.0"))  # seconds

# ── Schema versioning ─────────────────────────────────────────────────────────

# Increment when a new migration is added to _MIGRATIONS below.
CURRENT_SCHEMA_VERSION = 3

# Forward-only migrations: version_number → list of SQL DDL/DML statements.
# Each entry brings the DB from (version - 1) to version.
# Rules:
#   - Append-only; never modify or remove an existing entry.
#   - Statements must be idempotent (ALTER TABLE … ADD COLUMN is safe; duplicate
#     column errors are swallowed to handle re-runs).
#   - No data-destructive migrations (no DROP COLUMN, no DROP TABLE).
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        # Add 'source' to safety_events so callers can record which component
        # fired the event (e.g. "strategy_evolver", "adaptive_thresholds").
        "ALTER TABLE safety_events ADD COLUMN source TEXT NOT NULL DEFAULT ''",
    ],
    3: [
        # Phase 4 intelligence tables — Resolution Memory, Pattern Intelligence,
        # Incident Graph, Service Dependencies, Change Impact.
        """CREATE TABLE IF NOT EXISTS resolution_memories (
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
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rm_service ON resolution_memories(service)",
        "CREATE INDEX IF NOT EXISTS idx_rm_type ON resolution_memories(incident_type)",
        "CREATE INDEX IF NOT EXISTS idx_rm_status ON resolution_memories(validation_status)",
        "CREATE INDEX IF NOT EXISTS idx_rm_recorded ON resolution_memories(recorded_at DESC)",
        """CREATE TABLE IF NOT EXISTS operational_patterns (
            pattern_id          TEXT PRIMARY KEY,
            symptom_signature   TEXT NOT NULL DEFAULT '',
            incident_type       TEXT NOT NULL DEFAULT '',
            services            TEXT NOT NULL DEFAULT '[]',
            canonical_symptoms  TEXT NOT NULL DEFAULT '[]',
            occurrence_count    INTEGER NOT NULL DEFAULT 1,
            success_count       INTEGER NOT NULL DEFAULT 0,
            first_seen          TEXT NOT NULL DEFAULT '',
            last_seen           TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_op_type ON operational_patterns(incident_type)",
        "CREATE INDEX IF NOT EXISTS idx_op_count ON operational_patterns(occurrence_count DESC)",
        """CREATE TABLE IF NOT EXISTS incident_graph_nodes (
            node_id      TEXT NOT NULL,
            incident_id  TEXT NOT NULL,
            node_type    TEXT NOT NULL DEFAULT '',
            label        TEXT NOT NULL DEFAULT '',
            service      TEXT NOT NULL DEFAULT '',
            properties   TEXT NOT NULL DEFAULT '{}',
            recorded_at  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (node_id, incident_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ign_incident ON incident_graph_nodes(incident_id)",
        "CREATE INDEX IF NOT EXISTS idx_ign_service ON incident_graph_nodes(service)",
        """CREATE TABLE IF NOT EXISTS incident_graph_edges (
            edge_id        TEXT PRIMARY KEY,
            incident_id    TEXT NOT NULL DEFAULT '',
            source_node_id TEXT NOT NULL DEFAULT '',
            target_node_id TEXT NOT NULL DEFAULT '',
            relationship   TEXT NOT NULL DEFAULT '',
            weight         REAL NOT NULL DEFAULT 1.0,
            properties     TEXT NOT NULL DEFAULT '{}',
            recorded_at    TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ige_incident ON incident_graph_edges(incident_id)",
        "CREATE INDEX IF NOT EXISTS idx_ige_source ON incident_graph_edges(source_node_id)",
        """CREATE TABLE IF NOT EXISTS service_dependencies (
            dep_id         TEXT PRIMARY KEY,
            source_service TEXT NOT NULL DEFAULT '',
            target_service TEXT NOT NULL DEFAULT '',
            dep_type       TEXT NOT NULL DEFAULT 'runtime',
            strength       REAL NOT NULL DEFAULT 0.1,
            observed_count INTEGER NOT NULL DEFAULT 1,
            first_seen     TEXT NOT NULL DEFAULT '',
            last_seen      TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sd_source ON service_dependencies(source_service)",
        "CREATE INDEX IF NOT EXISTS idx_sd_target ON service_dependencies(target_service)",
        """CREATE TABLE IF NOT EXISTS changes (
            change_id    TEXT PRIMARY KEY,
            service      TEXT NOT NULL DEFAULT '',
            change_type  TEXT NOT NULL DEFAULT '',
            deployed_at  TEXT NOT NULL DEFAULT '',
            description  TEXT NOT NULL DEFAULT '',
            deployed_by  TEXT NOT NULL DEFAULT '',
            metadata     TEXT NOT NULL DEFAULT '{}',
            recorded_at  TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ch_service ON changes(service)",
        "CREATE INDEX IF NOT EXISTS idx_ch_deployed ON changes(deployed_at DESC)",
        """CREATE TABLE IF NOT EXISTS change_incident_links (
            link_id          TEXT PRIMARY KEY,
            change_id        TEXT NOT NULL DEFAULT '',
            incident_id      TEXT NOT NULL DEFAULT '',
            investigation_id TEXT NOT NULL DEFAULT '',
            impact_score     REAL NOT NULL DEFAULT 0,
            link_reason      TEXT NOT NULL DEFAULT '',
            linked_at        TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cil_change ON change_incident_links(change_id)",
        "CREATE INDEX IF NOT EXISTS idx_cil_incident ON change_incident_links(incident_id)",
        "CREATE INDEX IF NOT EXISTS idx_cil_investigation ON change_incident_links(investigation_id)",
    ],
}

# ── Replay meta validation ─────────────────────────────────────────────────────

# Fields that must be present and non-empty in the reflection dict; rows with
# missing required fields are logged and dropped before enqueue.
_REPLAY_META_REQUIRED: frozenset[str] = frozenset({"investigation_id"})

# ── Schema (base — version 1) ─────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS enriched_receipts (
    receipt_id       TEXT NOT NULL,
    investigation_id TEXT NOT NULL,
    phase            TEXT NOT NULL DEFAULT '',
    worker           TEXT NOT NULL DEFAULT '',
    action           TEXT NOT NULL DEFAULT '',
    intent_summary   TEXT NOT NULL DEFAULT '',
    params_json      TEXT NOT NULL DEFAULT '{}',
    called_at_ms     REAL NOT NULL DEFAULT 0,
    latency_ms       REAL NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'success',
    result_count     INTEGER NOT NULL DEFAULT 0,
    signal_facts_json TEXT NOT NULL DEFAULT '[]',
    noise_ratio      REAL NOT NULL DEFAULT 0,
    raw_preview      TEXT NOT NULL DEFAULT '',
    error_msg        TEXT NOT NULL DEFAULT '',
    hyp_deltas_json  TEXT NOT NULL DEFAULT '[]',
    confidence_before REAL NOT NULL DEFAULT 0,
    confidence_after  REAL NOT NULL DEFAULT 0,
    recorded_at      REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (investigation_id, receipt_id)
);
CREATE INDEX IF NOT EXISTS idx_receipts_inv ON enriched_receipts(investigation_id);
CREATE INDEX IF NOT EXISTS idx_receipts_ts  ON enriched_receipts(recorded_at DESC);

CREATE TABLE IF NOT EXISTS weight_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL DEFAULT (unixepoch()),
    incident_type   TEXT    NOT NULL DEFAULT '',
    step_label      TEXT    NOT NULL DEFAULT '',
    service         TEXT    NOT NULL DEFAULT '',
    weight_before   REAL    NOT NULL DEFAULT 1.0,
    weight_after    REAL    NOT NULL DEFAULT 1.0,
    quality_signal  REAL    NOT NULL DEFAULT 0,
    calls           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wh_type_step ON weight_history(incident_type, step_label);
CREATE INDEX IF NOT EXISTS idx_wh_ts        ON weight_history(ts DESC);

CREATE TABLE IF NOT EXISTS convergence_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL DEFAULT (unixepoch()),
    investigation_id TEXT   NOT NULL DEFAULT '',
    ece             REAL    NOT NULL DEFAULT 0,
    total_samples   INTEGER NOT NULL DEFAULT 0,
    mean_confidence REAL    NOT NULL DEFAULT 0,
    bins_with_data  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ch_ts ON convergence_history(ts DESC);

CREATE TABLE IF NOT EXISTS pattern_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL DEFAULT (unixepoch()),
    pattern_type    TEXT    NOT NULL DEFAULT '',
    prediction_id   TEXT    NOT NULL DEFAULT '',
    service         TEXT    NOT NULL DEFAULT '',
    outcome         TEXT    NOT NULL DEFAULT '',
    confidence      REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ph_type ON pattern_history(pattern_type);
CREATE INDEX IF NOT EXISTS idx_ph_ts   ON pattern_history(ts DESC);

CREATE TABLE IF NOT EXISTS safety_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL DEFAULT (unixepoch()),
    event_type      TEXT    NOT NULL DEFAULT '',
    threshold_name  TEXT    NOT NULL DEFAULT '',
    old_value       REAL    NOT NULL DEFAULT 0,
    new_value       REAL    NOT NULL DEFAULT 0,
    drift_fraction  REAL    NOT NULL DEFAULT 0,
    context         TEXT    NOT NULL DEFAULT '',
    details_json    TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_se_ts   ON safety_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_se_type ON safety_events(event_type);

CREATE TABLE IF NOT EXISTS replay_meta (
    investigation_id TEXT    PRIMARY KEY,
    incident_id      TEXT    NOT NULL DEFAULT '',
    initial_quality  REAL    NOT NULL DEFAULT 0,
    final_quality    REAL    NOT NULL DEFAULT 0,
    rounds_run       INTEGER NOT NULL DEFAULT 1,
    stuck            INTEGER NOT NULL DEFAULT 0,
    confidence_raw   INTEGER NOT NULL DEFAULT 0,
    confidence_cal   INTEGER NOT NULL DEFAULT 0,
    experience_matches INTEGER NOT NULL DEFAULT 0,
    learning_updated INTEGER NOT NULL DEFAULT 0,
    experience_stored INTEGER NOT NULL DEFAULT 0,
    elapsed_ms       REAL    NOT NULL DEFAULT 0,
    corrections_json TEXT    NOT NULL DEFAULT '[]',
    narrative        TEXT    NOT NULL DEFAULT '',
    recorded_at      REAL    NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_rm_ts ON replay_meta(recorded_at DESC);

CREATE TABLE IF NOT EXISTS kv_state (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL DEFAULT 'null',
    updated_at  REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT 'null'
);
"""

# ── Write item ─────────────────────────────────────────────────────────────────

@dataclass
class _WriteItem:
    sql: str
    params: tuple


# ── Engine ────────────────────────────────────────────────────────────────────

class OpsPersistence:
    """SQLite-backed durable intelligence persistence with async write queue."""

    def __init__(self, db_path: str = OPS_DB_PATH) -> None:
        self._db_path = db_path
        self._q: queue.Queue[_WriteItem | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._ready = False
        self._lock = threading.Lock()

        # Queue saturation counters — incremented under _lock
        self._drops: int = 0          # total writes dropped (queue full)
        self._q_sat_events: int = 0   # number of distinct queue-full events

    # ── Startup / shutdown ──────────────────────────────────────────────────

    def start(self) -> None:
        """Initialise DB, run integrity check, start background writer."""
        if not OPS_DB_ENABLED:
            logger.info("Ops persistence disabled (OPS_DB_ENABLED=false)")
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
            # Integrity check before schema: a totally corrupt (non-SQLite) file
            # crashes executescript(); quarantine it first so _ensure_schema() gets
            # a clean slate.
            if not self._integrity_ok():
                self._quarantine_db()
            self._ensure_schema()
            self._run_migrations()
            self._run_retention_cleanup()
            self._thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="ops-persistence-writer"
            )
            self._thread.start()
            self._ready = True
            logger.info("Ops persistence ready: %s (schema v%d)", self._db_path, CURRENT_SCHEMA_VERSION)
        except Exception as exc:
            logger.warning("Ops persistence startup failed (non-fatal): %s", exc)

    def stop(self) -> None:
        if self._thread and self._ready:
            self._q.put(None)  # poison pill
            self._thread.join(timeout=5)

    # ── Schema ────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)

    def _integrity_ok(self) -> bool:
        try:
            with self._connect() as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return row and row[0] == "ok"
        except Exception as exc:
            logger.warning("Integrity check failed: %s", exc)
            return False

    def _quarantine_db(self) -> None:
        ts = int(time.time())
        corrupt = f"{self._db_path}.corrupt.{ts}"
        try:
            os.rename(self._db_path, corrupt)
            logger.error("Corrupt ops DB quarantined to %s — starting fresh", corrupt)
        except OSError:
            pass

    # ── Schema migrations ─────────────────────────────────────────────────

    def _get_schema_version(self) -> int:
        """Return persisted schema version, or 1 (base) if not recorded."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value_json FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row:
                    return int(json.loads(row["value_json"]))
        except Exception:
            pass
        return 1  # base schema; schema_meta may be newly created

    def _set_schema_version(self, version: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value_json) VALUES ('schema_version', ?)",
                (json.dumps(version),),
            )
            conn.commit()

    def _run_migrations(self) -> None:
        """Apply all pending forward-only migrations in version order."""
        current = self._get_schema_version()
        if current >= CURRENT_SCHEMA_VERSION:
            return
        for v in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            stmts = _MIGRATIONS.get(v, [])
            if stmts:
                with self._connect() as conn:
                    for stmt in stmts:
                        try:
                            conn.execute(stmt)
                        except sqlite3.OperationalError as exc:
                            if "duplicate column name" in str(exc).lower():
                                pass  # idempotent — column already present
                            else:
                                raise
                    conn.commit()
            self._set_schema_version(v)
        logger.info(
            "Schema migrated: v%d → v%d", current, CURRENT_SCHEMA_VERSION
        )

    # ── Background writer ─────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        batch: list[_WriteItem] = []
        while True:
            try:
                item = self._q.get(timeout=_BATCH_DELAY)
                if item is None:          # poison pill
                    if batch:
                        self._flush(batch)
                    return
                batch.append(item)
                if len(batch) >= _BATCH_SIZE:
                    self._flush(batch)
                    batch = []
            except queue.Empty:
                if batch:
                    self._flush(batch)
                    batch = []

    def _flush(self, batch: list[_WriteItem]) -> None:
        try:
            with self._connect() as conn:
                for item in batch:
                    try:
                        conn.execute(item.sql, item.params)
                    except sqlite3.IntegrityError:
                        pass  # duplicate PK — idempotent
                conn.commit()
        except Exception as exc:
            logger.warning("Ops persistence flush error (non-fatal): %s", exc)

    def _enqueue(self, sql: str, params: tuple) -> None:
        if not self._ready:
            return
        try:
            self._q.put_nowait(_WriteItem(sql, params))
        except queue.Full:
            with self._lock:
                self._drops += 1
                self._q_sat_events += 1
            logger.debug(
                "Ops persistence queue full — dropping write (total drops: %d)",
                self._drops,
            )

    # ── Retention cleanup ─────────────────────────────────────────────────

    def _run_retention_cleanup(self) -> None:
        """Delete rows older than retention windows. Runs synchronously on startup."""
        now = time.time()
        cuts = {
            "enriched_receipts":  now - _RETENTION_RECEIPTS_DAYS * 86400,
            "weight_history":     now - _RETENTION_HISTORY_DAYS * 86400,
            "convergence_history":now - _RETENTION_HISTORY_DAYS * 86400,
            "pattern_history":    now - _RETENTION_HISTORY_DAYS * 86400,
            "safety_events":      now - _RETENTION_SAFETY_DAYS * 86400,
            "replay_meta":        now - _RETENTION_HISTORY_DAYS * 86400,
        }
        try:
            with self._connect() as conn:
                for table, cutoff in cuts.items():
                    col = "recorded_at" if table in ("enriched_receipts", "replay_meta") else "ts"
                    conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                conn.commit()
        except Exception as exc:
            logger.warning("Retention cleanup error (non-fatal): %s", exc)

    # ── Public write API ─────────────────────────────────────────────────

    def persist_receipt(self, receipt: Any) -> None:
        """Persist one EnrichedToolReceipt (non-blocking)."""
        self._enqueue(
            """INSERT OR REPLACE INTO enriched_receipts
               (receipt_id, investigation_id, phase, worker, action, intent_summary,
                params_json, called_at_ms, latency_ms, status, result_count,
                signal_facts_json, noise_ratio, raw_preview, error_msg,
                hyp_deltas_json, confidence_before, confidence_after, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                receipt.receipt_id,
                receipt.investigation_id,
                receipt.phase,
                receipt.worker,
                receipt.action,
                receipt.intent_summary,
                json.dumps(receipt.params, default=str),
                receipt.called_at_ms,
                receipt.latency_ms,
                receipt.status,
                receipt.result_count,
                json.dumps([{"category": f.category, "text": f.text, "weight": f.weight}
                             for f in receipt.signal_facts], default=str),
                receipt.noise_ratio,
                receipt.raw_preview[:500] if receipt.raw_preview else "",
                receipt.error_msg,
                json.dumps([{"name": d.name, "score_before": d.score_before,
                              "score_after": d.score_after}
                             for d in receipt.hypothesis_deltas], default=str),
                receipt.confidence_before,
                receipt.confidence_after,
                time.time(),
            ),
        )

    def persist_weight_change(
        self,
        incident_type: str,
        step_label: str,
        weight_before: float,
        weight_after: float,
        quality_signal: float,
        calls: int,
        service: str = "",
    ) -> None:
        self._enqueue(
            """INSERT INTO weight_history
               (ts, incident_type, step_label, service, weight_before, weight_after,
                quality_signal, calls)
               VALUES (?,?,?,?,?,?,?,?)""",
            (time.time(), incident_type, step_label, service,
             round(weight_before, 4), round(weight_after, 4),
             round(quality_signal, 4), calls),
        )

    def persist_convergence_snapshot(
        self,
        investigation_id: str,
        ece: float,
        total_samples: int,
        mean_confidence: float,
        bins_with_data: int,
    ) -> None:
        self._enqueue(
            """INSERT INTO convergence_history
               (ts, investigation_id, ece, total_samples, mean_confidence, bins_with_data)
               VALUES (?,?,?,?,?,?)""",
            (time.time(), investigation_id, round(ece, 4), total_samples,
             round(mean_confidence, 2), bins_with_data),
        )

    def persist_pattern_event(
        self,
        pattern_type: str,
        prediction_id: str,
        service: str,
        outcome: str,
        confidence: float,
    ) -> None:
        self._enqueue(
            """INSERT INTO pattern_history
               (ts, pattern_type, prediction_id, service, outcome, confidence)
               VALUES (?,?,?,?,?,?)""",
            (time.time(), pattern_type, prediction_id, service, outcome, round(confidence, 4)),
        )

    def persist_safety_event(
        self,
        event_type: str,
        threshold_name: str = "",
        old_value: float = 0.0,
        new_value: float = 0.0,
        drift_fraction: float = 0.0,
        context: str = "",
        details: dict | None = None,
        source: str = "",
    ) -> None:
        self._enqueue(
            """INSERT INTO safety_events
               (ts, event_type, threshold_name, old_value, new_value,
                drift_fraction, context, details_json, source)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (time.time(), event_type, threshold_name,
             round(old_value, 4), round(new_value, 4),
             round(drift_fraction, 4), context,
             json.dumps(details or {}, default=str),
             source),
        )

    def persist_replay_meta(self, reflection_dict: dict) -> None:
        """Persist a HarnessReflection.to_dict() payload.

        Drops the row and logs a warning if any required field is missing or
        empty — prevents silent insertion of unidentifiable records.
        """
        missing = [f for f in _REPLAY_META_REQUIRED if not reflection_dict.get(f)]
        if missing:
            logger.warning(
                "persist_replay_meta skipped — missing required fields: %s", missing
            )
            return

        d = reflection_dict
        self._enqueue(
            """INSERT OR REPLACE INTO replay_meta
               (investigation_id, incident_id, initial_quality, final_quality,
                rounds_run, stuck, confidence_raw, confidence_cal,
                experience_matches, learning_updated, experience_stored,
                elapsed_ms, corrections_json, narrative, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d.get("investigation_id", ""),
                d.get("incident_id", ""),
                d.get("initial_quality", 0.0),
                d.get("final_quality", 0.0),
                d.get("rounds_run", 1),
                int(d.get("stuck", False)),
                d.get("confidence_raw", 0),
                d.get("confidence_calibrated", 0),
                d.get("experience_matches", 0),
                int(d.get("learning_updated", False)),
                int(d.get("experience_stored", False)),
                d.get("elapsed_ms", 0.0),
                json.dumps(d.get("corrections", []), default=str),
                d.get("narrative", ""),
                time.time(),
            ),
        )

    def set_state(self, key: str, value: Any) -> None:
        """Persist a key-value intelligence state flag."""
        self._enqueue(
            """INSERT OR REPLACE INTO kv_state (key, value_json, updated_at)
               VALUES (?,?,?)""",
            (key, json.dumps(value, default=str), time.time()),
        )

    # ── Public read API ──────────────────────────────────────────────────

    def load_receipts_for_investigation(self, investigation_id: str) -> list[dict]:
        """Load all persisted receipts for an investigation (for recovery)."""
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM enriched_receipts WHERE investigation_id = ? ORDER BY called_at_ms",
                    (investigation_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("load_receipts failed: %s", exc)
            return []

    def load_recent_replay_meta(self, limit: int = 100) -> list[dict]:
        """Load most recent HarnessReflection records for reporting."""
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM replay_meta ORDER BY recorded_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["corrections"] = json.loads(d.pop("corrections_json", "[]"))
                result.append(d)
            return result
        except Exception as exc:
            logger.warning("load_recent_replay_meta failed: %s", exc)
            return []

    def load_convergence_history(self, limit: int = 200) -> list[dict]:
        """Load ECE time-series for reporting."""
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT ts, ece, total_samples, mean_confidence FROM convergence_history ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("load_convergence_history failed: %s", exc)
            return []

    def load_recent_safety_events(self, limit: int = 50) -> list[dict]:
        """Load recent safety events for health reporting."""
        if not self._ready:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM safety_events ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("load_recent_safety_events failed: %s", exc)
            return []

    def get_state(self, key: str, default: Any = None) -> Any:
        """Retrieve a key-value state flag."""
        if not self._ready:
            return default
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value_json FROM kv_state WHERE key = ?", (key,)
                ).fetchone()
            return json.loads(row["value_json"]) if row else default
        except Exception:
            return default

    def get_health(self) -> dict:
        """Return row counts, queue metrics, and schema version for monitoring."""
        counts = {}
        schema_ver = None
        if self._ready:
            try:
                with self._connect() as conn:
                    for t in ("enriched_receipts", "weight_history", "convergence_history",
                              "pattern_history", "safety_events", "replay_meta"):
                        row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
                        counts[t] = row["n"] if row else 0
                schema_ver = self._get_schema_version()
            except Exception:
                pass

        with self._lock:
            drops = self._drops
            sat_events = self._q_sat_events

        return {
            "enabled": OPS_DB_ENABLED,
            "ready": self._ready,
            "db_path": self._db_path,
            "schema_version": schema_ver,
            "queue_depth": self._q.qsize(),
            "dropped_writes": drops,
            "queue_saturation_events": sat_events,
            "row_counts": counts,
        }


# ── Process singleton ─────────────────────────────────────────────────────────

_instance: OpsPersistence | None = None
_instance_lock = threading.Lock()


def get_ops_store() -> OpsPersistence:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = OpsPersistence()
                _instance.start()
    return _instance
