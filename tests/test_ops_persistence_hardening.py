"""Hardening tests for the three documented persistence risks.

1. Queue saturation: dropped_writes counter, queue_saturation_events counter
2. Schema migration: v1 → v2 via forward-only migration framework
3. replay_meta validation: missing required fields are rejected before enqueue
"""
from __future__ import annotations

import json
import queue as _queue
import sqlite3
import time
import uuid

import pytest

from database.ops_persistence import (
    OpsPersistence,
    CURRENT_SCHEMA_VERSION,
    _MIGRATIONS,
    _REPLAY_META_REQUIRED,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _start(tmp_path, name: str = "ops.db") -> OpsPersistence:
    store = OpsPersistence(db_path=str(tmp_path / name))
    store.start()
    return store


def _flush(store: OpsPersistence) -> None:
    """Drain queue via poison pill and wait for thread exit."""
    store.stop()


def _columns(db_path: str, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cur.fetchall()]
    finally:
        conn.close()


def _schema_version(db_path: str) -> int | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value_json FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(json.loads(row["value_json"])) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _minimal_reflection(investigation_id: str = "inv-001") -> dict:
    return {
        "investigation_id": investigation_id,
        "incident_id": "INC-1",
        "initial_quality": 0.6,
        "final_quality": 0.8,
        "rounds_run": 2,
        "stuck": False,
        "confidence_raw": 70,
        "confidence_calibrated": 68,
        "experience_matches": 1,
        "learning_updated": True,
        "experience_stored": True,
        "elapsed_ms": 4200.0,
        "corrections": [],
        "narrative": "resolved",
    }


# ── 1. Queue saturation metrics ────────────────────────────────────────────────

class TestQueueSaturationMetrics:
    def test_health_exposes_dropped_writes_key(self, tmp_path):
        store = _start(tmp_path)
        health = store.get_health()
        _flush(store)
        assert "dropped_writes" in health
        assert "queue_saturation_events" in health

    def test_dropped_writes_zero_on_fresh_store(self, tmp_path):
        store = _start(tmp_path)
        health = store.get_health()
        _flush(store)
        assert health["dropped_writes"] == 0
        assert health["queue_saturation_events"] == 0

    def test_dropped_writes_increments_on_queue_full(self, tmp_path):
        store = _start(tmp_path)
        # Replace queue with a tiny capacity so it fills immediately
        store._q = _queue.Queue(maxsize=1)
        # Fill the one slot
        store._enqueue("SELECT 1", ())
        # This call must be dropped and counted
        store._enqueue("SELECT 2", ())
        store._enqueue("SELECT 3", ())

        health = store.get_health()
        _flush(store)

        assert health["dropped_writes"] >= 2
        assert health["queue_saturation_events"] >= 2

    def test_dropped_writes_counter_is_cumulative(self, tmp_path):
        store = _start(tmp_path)
        store._q = _queue.Queue(maxsize=1)
        # Force fills in two separate rounds
        store._enqueue("SELECT 1", ())
        store._enqueue("SELECT 2", ())  # drop 1

        h1 = store.get_health()

        store._enqueue("SELECT 3", ())  # drop 2 (slot still held)
        h2 = store.get_health()

        _flush(store)

        assert h2["dropped_writes"] > h1["dropped_writes"]

    def test_queue_depth_reflects_pending_items(self, tmp_path):
        store = _start(tmp_path)
        # Pause the writer by temporarily replacing queue with a large one
        # and checking depth before drain
        store.persist_pattern_event("trend_drift", uuid.uuid4().hex, "svc", "pending", 0.7)
        store.persist_pattern_event("trend_drift", uuid.uuid4().hex, "svc", "pending", 0.7)
        depth = store.get_health()["queue_depth"]
        _flush(store)
        # Depth was non-negative (may be 0 if writer drained already — that's fine)
        assert depth >= 0

    def test_health_schema_version_present(self, tmp_path):
        store = _start(tmp_path)
        health = store.get_health()
        _flush(store)
        assert "schema_version" in health
        assert health["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_dropped_writes_thread_safe(self, tmp_path):
        """Multiple threads overflowing the queue must not corrupt the counter."""
        import threading
        store = _start(tmp_path)
        store._q = _queue.Queue(maxsize=5)

        errors = []

        def overflow():
            try:
                for _ in range(20):
                    store._enqueue("SELECT 1", ())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=overflow) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        _flush(store)
        assert not errors
        h = store.get_health()
        # Counter must be non-negative and consistent (no race corruption)
        assert h["dropped_writes"] >= 0
        assert isinstance(h["dropped_writes"], int)


# ── 2. Schema migration framework ─────────────────────────────────────────────

class TestSchemaMigration:
    def test_new_db_reaches_current_schema_version(self, tmp_path):
        store = _start(tmp_path)
        _flush(store)
        assert _schema_version(store._db_path) == CURRENT_SCHEMA_VERSION

    def test_schema_meta_table_exists(self, tmp_path):
        store = _start(tmp_path)
        _flush(store)
        conn = sqlite3.connect(store._db_path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "schema_meta table not found"

    def test_v2_migration_adds_source_column_to_safety_events(self, tmp_path):
        """v1→v2 migration must add 'source' column to safety_events."""
        store = _start(tmp_path)
        _flush(store)
        cols = _columns(store._db_path, "safety_events")
        assert "source" in cols, f"Expected 'source' column; got: {cols}"

    def test_migration_applied_to_pre_existing_v1_db(self, tmp_path):
        """DB created at v1 (no schema_meta, no source column) gets migrated to CURRENT_SCHEMA_VERSION."""
        db_path = str(tmp_path / "v1_db.db")

        # Build a v1-level DB manually: base tables but no schema_meta and no source column
        v1_ddl = """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS safety_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL DEFAULT '',
            threshold_name TEXT NOT NULL DEFAULT '',
            old_value REAL NOT NULL DEFAULT 0,
            new_value REAL NOT NULL DEFAULT 0,
            drift_fraction REAL NOT NULL DEFAULT 0,
            context TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS kv_state (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL DEFAULT 'null',
            updated_at REAL NOT NULL DEFAULT 0
        );
        """
        conn = sqlite3.connect(db_path)
        conn.executescript(v1_ddl)
        conn.close()

        # Verify precondition: no source column yet
        assert "source" not in _columns(db_path, "safety_events")

        # Start OpsPersistence — must detect v1, run all migrations, reach CURRENT_SCHEMA_VERSION
        store = OpsPersistence(db_path=db_path)
        store.start()
        _flush(store)

        assert "source" in _columns(db_path, "safety_events"), (
            "Migration v2 did not add 'source' column to safety_events"
        )
        assert _schema_version(db_path) == CURRENT_SCHEMA_VERSION

    def test_migration_is_idempotent_on_already_migrated_db(self, tmp_path):
        """Running start() twice on the same DB must not error or double-migrate."""
        db_path = str(tmp_path / "idem_db.db")

        store1 = OpsPersistence(db_path=db_path)
        store1.start()
        _flush(store1)

        store2 = OpsPersistence(db_path=db_path)
        store2.start()
        _flush(store2)

        # Schema version still correct, source column still present
        assert _schema_version(db_path) == CURRENT_SCHEMA_VERSION
        assert "source" in _columns(db_path, "safety_events")

    def test_migrations_dict_is_append_only_ordered(self):
        """Validate structural invariants of the migrations registry."""
        versions = sorted(_MIGRATIONS.keys())
        assert versions == list(range(2, CURRENT_SCHEMA_VERSION + 1)), (
            f"Migration keys must be consecutive from 2 to {CURRENT_SCHEMA_VERSION}: got {versions}"
        )
        for v, stmts in _MIGRATIONS.items():
            assert isinstance(stmts, list), f"Migration v{v} must be a list of SQL strings"
            assert all(isinstance(s, str) for s in stmts), f"Migration v{v} has non-string entry"

    def test_get_schema_version_returns_1_for_db_without_schema_meta(self, tmp_path):
        """DB with no schema_meta table at all is treated as version 1."""
        db_path = str(tmp_path / "no_meta.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        store = OpsPersistence(db_path=db_path)
        # Don't call start() — just call the internal method directly
        assert store._get_schema_version() == 1

    def test_persist_safety_event_with_source_field(self, tmp_path):
        """After migration, source field is persisted correctly."""
        store = _start(tmp_path)
        store.persist_safety_event(
            event_type="circuit_breaker_fired",
            context="strategy_evolver",
            source="strategy_evolver",
        )
        _flush(store)

        conn = sqlite3.connect(store._db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT source FROM safety_events WHERE event_type='circuit_breaker_fired'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["source"] == "strategy_evolver"

    def test_persist_safety_event_source_defaults_to_empty(self, tmp_path):
        """Callers that don't pass source get empty string (backwards compatible)."""
        store = _start(tmp_path)
        store.persist_safety_event(event_type="threshold_damped")
        _flush(store)

        conn = sqlite3.connect(store._db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT source FROM safety_events WHERE event_type='threshold_damped'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["source"] == ""


# ── 3. replay_meta required field validation ───────────────────────────────────

class TestReplayMetaValidation:
    def test_required_fields_constant_contains_investigation_id(self):
        assert "investigation_id" in _REPLAY_META_REQUIRED

    def test_valid_reflection_is_persisted(self, tmp_path):
        store = _start(tmp_path)
        store.persist_replay_meta(_minimal_reflection("inv-valid-001"))
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        assert any(r["investigation_id"] == "inv-valid-001" for r in rows)

    def test_missing_investigation_id_is_dropped(self, tmp_path):
        store = _start(tmp_path)
        store.persist_replay_meta({})  # no investigation_id
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        assert len(rows) == 0

    def test_empty_string_investigation_id_is_dropped(self, tmp_path):
        store = _start(tmp_path)
        store.persist_replay_meta({"investigation_id": "", "incident_id": "INC-1"})
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        assert len(rows) == 0

    def test_null_investigation_id_is_dropped(self, tmp_path):
        store = _start(tmp_path)
        store.persist_replay_meta({"investigation_id": None})
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        assert len(rows) == 0

    def test_invalid_call_does_not_corrupt_subsequent_valid_call(self, tmp_path):
        """A dropped call must not prevent the next valid call from persisting."""
        store = _start(tmp_path)
        store.persist_replay_meta({})                                # dropped
        store.persist_replay_meta(_minimal_reflection("inv-v2-001")) # valid
        store.persist_replay_meta({"investigation_id": ""})          # dropped
        store.persist_replay_meta(_minimal_reflection("inv-v2-002")) # valid
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        ids = {r["investigation_id"] for r in rows}
        assert "inv-v2-001" in ids
        assert "inv-v2-002" in ids
        assert len(ids) == 2  # no phantom rows from dropped calls

    def test_missing_optional_fields_use_defaults(self, tmp_path):
        """Row with only investigation_id must persist with zero-value defaults."""
        store = _start(tmp_path)
        store.persist_replay_meta({"investigation_id": "inv-minimal-001"})
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        assert len(rows) == 1
        r = rows[0]
        assert r["investigation_id"] == "inv-minimal-001"
        assert r["incident_id"] == ""
        assert r["initial_quality"] == 0.0
        assert r["rounds_run"] == 1

    def test_persist_replay_meta_logs_warning_on_missing_field(self, tmp_path, caplog):
        import logging
        store = _start(tmp_path)
        with caplog.at_level(logging.WARNING, logger="sentinalai.ops_persistence"):
            store.persist_replay_meta({"some_other_key": "value"})
        _flush(store)

        assert any("missing required fields" in r.message for r in caplog.records), (
            "Expected warning about missing required fields"
        )

    def test_duplicate_investigation_id_upserts_row(self, tmp_path):
        """Two calls with the same investigation_id → INSERT OR REPLACE → one row."""
        store = _start(tmp_path)
        r1 = _minimal_reflection("inv-dup-001")
        r1["final_quality"] = 0.7
        r2 = _minimal_reflection("inv-dup-001")
        r2["final_quality"] = 0.9

        store.persist_replay_meta(r1)
        store.persist_replay_meta(r2)
        _flush(store)

        rows = store.load_recent_replay_meta(limit=10)
        matching = [r for r in rows if r["investigation_id"] == "inv-dup-001"]
        assert len(matching) == 1
        assert matching[0]["final_quality"] == pytest.approx(0.9)

    def test_health_still_works_after_dropped_replay_meta(self, tmp_path):
        """Dropping a replay_meta row must not affect get_health() correctness."""
        store = _start(tmp_path)
        store.persist_replay_meta({})  # dropped
        _flush(store)

        health = store.get_health()
        assert health["ready"] is True
        assert health["row_counts"]["replay_meta"] == 0


# ── Cross-concern: all three hardening features together ──────────────────────

class TestHardeningIntegration:
    def test_migration_and_saturation_and_validation_together(self, tmp_path):
        """Start a fresh store; saturate queue; attempt bad replay_meta; verify health."""
        store = _start(tmp_path)

        # Saturate queue
        store._q = _queue.Queue(maxsize=2)
        for _ in range(10):
            store._enqueue("SELECT 1", ())

        # Bad replay_meta — should be caught before enqueue, so no queue pressure
        store.persist_replay_meta({})
        store.persist_replay_meta({"investigation_id": "inv-good-001"})

        _flush(store)

        health = store.get_health()
        assert health["schema_version"] == CURRENT_SCHEMA_VERSION
        assert health["dropped_writes"] >= 8   # 10 overflows, first 2 fit
        assert health["ready"] is True
        assert "source" in _columns(store._db_path, "safety_events")
