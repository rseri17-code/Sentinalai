"""E2E validation for durable operational intelligence persistence.

Covers every write path, restart survival, and replay consistency for the
SQLite ops store (database/ops_persistence.py) and all its producers.

Each test uses an isolated OpsPersistence instance pointed at a temp DB file;
the process singleton is never touched.

Flush strategy: call store.stop() to drain the async write queue (the poison
pill causes the writer thread to flush the pending batch before exiting).
Reads work after stop() because _ready remains True.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from database.ops_persistence import OpsPersistence
from supervisor.tool_transparency import (
    EnrichedToolReceipt, SignalFact, HypothesisDelta, ToolTransparencyEmitter,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _start_store(tmp_path) -> OpsPersistence:
    db = str(tmp_path / "ops_e2e.db")
    store = OpsPersistence(db_path=db)
    store.start()
    return store


def _flush(store: OpsPersistence) -> None:
    """Drain queue to disk by stopping the writer (poison pill → flush → exit)."""
    store.stop()


def _direct_query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    """Read directly from SQLite file, bypassing the OpsPersistence layer."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _make_receipt(investigation_id: str = "inv-001") -> EnrichedToolReceipt:
    return EnrichedToolReceipt(
        receipt_id=uuid.uuid4().hex[:16],
        investigation_id=investigation_id,
        phase="collect",
        worker="log_worker",
        action="fetch_logs",
        intent_summary="Collect fetch_logs for api-gateway",
        params={"service": "api-gateway", "minutes": 30},
        called_at_ms=time.time() * 1000,
        latency_ms=142.5,
        status="success",
        result_count=3,
        signal_facts=[SignalFact("error", "OOM killed", 1.0)],
        noise_ratio=0.33,
        raw_preview='{"logs": [...]}',
        error_msg="",
        hypothesis_deltas=[HypothesisDelta("memory_leak", 0.4, 0.65)],
        confidence_before=40.0,
        confidence_after=65.0,
    )


@dataclass
class FakeDetection:
    service: str = "api-gateway"
    pattern_type: str = "trend_drift"
    severity: str = "LIKELY"
    metric: str = "error_rate"
    confidence: float = 0.72
    current_value: float = 0.005
    explanation: str = "Error rate rising"
    predicted_breach_hours: float | None = 4.0
    related_service: str = ""
    evidence: dict = field(default_factory=dict)


# ── Schema and startup tests ───────────────────────────────────────────────────

class TestSchemaInit:
    def test_all_seven_tables_created(self, tmp_path):
        store = _start_store(tmp_path)
        _flush(store)
        rows = _direct_query(
            store._db_path,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )
        names = {r["name"] for r in rows}
        expected = {
            "enriched_receipts", "weight_history", "convergence_history",
            "pattern_history", "safety_events", "replay_meta", "kv_state",
        }
        assert expected.issubset(names), f"Missing tables: {expected - names}"

    def test_wal_journal_mode(self, tmp_path):
        store = _start_store(tmp_path)
        _flush(store)
        rows = _direct_query(store._db_path, "PRAGMA journal_mode")
        assert rows[0]["journal_mode"] == "wal"

    def test_integrity_check_passes_on_fresh_db(self, tmp_path):
        store = _start_store(tmp_path)
        assert store._integrity_ok()
        _flush(store)

    def test_start_is_idempotent(self, tmp_path):
        store = _start_store(tmp_path)
        # Second start should not crash or duplicate tables
        store.start()
        _flush(store)
        rows = _direct_query(
            store._db_path,
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'",
        )
        assert rows[0]["n"] >= 7


# ── Receipt persistence ────────────────────────────────────────────────────────

class TestReceiptPersistence:
    def test_receipt_roundtrip(self, tmp_path):
        store = _start_store(tmp_path)
        receipt = _make_receipt("inv-rt-001")
        store.persist_receipt(receipt)
        _flush(store)

        rows = _direct_query(
            store._db_path,
            "SELECT * FROM enriched_receipts WHERE investigation_id = ?",
            ("inv-rt-001",),
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["receipt_id"] == receipt.receipt_id
        assert r["worker"] == "log_worker"
        assert r["action"] == "fetch_logs"
        assert r["phase"] == "collect"
        assert r["status"] == "success"
        assert abs(r["latency_ms"] - 142.5) < 0.01
        assert r["confidence_before"] == pytest.approx(40.0)
        assert r["confidence_after"] == pytest.approx(65.0)

    def test_params_json_stores_actual_params_not_signal_facts(self, tmp_path):
        """params_json must serialise receipt.params, not signal_facts (regression guard)."""
        store = _start_store(tmp_path)
        receipt = _make_receipt("inv-params-001")
        store.persist_receipt(receipt)
        _flush(store)

        rows = _direct_query(
            store._db_path,
            "SELECT params_json, signal_facts_json FROM enriched_receipts WHERE investigation_id = ?",
            ("inv-params-001",),
        )
        assert rows, "Row not found"
        import json
        params = json.loads(rows[0]["params_json"])
        # Must contain actual params keys, NOT signal-fact keys
        assert "service" in params, (
            f"params_json should contain {{service: ...}} but got: {params}"
        )
        assert "category" not in params, (
            f"params_json incorrectly contains signal_fact data: {params}"
        )

    def test_signal_facts_json_stored_correctly(self, tmp_path):
        store = _start_store(tmp_path)
        receipt = _make_receipt("inv-sf-001")
        store.persist_receipt(receipt)
        _flush(store)

        rows = _direct_query(
            store._db_path,
            "SELECT signal_facts_json FROM enriched_receipts WHERE investigation_id = ?",
            ("inv-sf-001",),
        )
        import json
        facts = json.loads(rows[0]["signal_facts_json"])
        assert isinstance(facts, list)
        assert facts[0]["category"] == "error"
        assert facts[0]["text"] == "OOM killed"

    def test_multiple_receipts_for_same_investigation(self, tmp_path):
        store = _start_store(tmp_path)
        inv_id = "inv-multi-001"
        for i in range(5):
            r = _make_receipt(inv_id)
            r.action = f"fetch_{i}"
            store.persist_receipt(r)
        _flush(store)

        rows = _direct_query(
            store._db_path,
            "SELECT COUNT(*) AS n FROM enriched_receipts WHERE investigation_id = ?",
            (inv_id,),
        )
        assert rows[0]["n"] == 5

    def test_duplicate_receipt_id_upserts_not_errors(self, tmp_path):
        store = _start_store(tmp_path)
        receipt = _make_receipt("inv-dup-001")
        store.persist_receipt(receipt)
        store.persist_receipt(receipt)  # same receipt_id
        _flush(store)

        rows = _direct_query(
            store._db_path,
            "SELECT COUNT(*) AS n FROM enriched_receipts WHERE receipt_id = ?",
            (receipt.receipt_id,),
        )
        assert rows[0]["n"] == 1  # INSERT OR REPLACE → one row


# ── Pattern history lifecycle ──────────────────────────────────────────────────

class TestPatternHistoryLifecycle:
    """Validates PredictionStore→pattern_history writes for all four outcomes."""

    def _make_store_with_ops(self, tmp_path):
        """Return (PredictionStore, OpsPersistence) with ops wired in."""
        from intelligence.prediction_store import PredictionStore
        ops = _start_store(tmp_path)
        pred_store = PredictionStore()
        return pred_store, ops

    def test_store_publishes_pending_event(self, tmp_path):
        pred_store, ops = self._make_store_with_ops(tmp_path)
        detection = FakeDetection()

        with patch("database.ops_persistence.get_ops_store", return_value=ops), \
             patch("database.persistence.get_engine", return_value=None):
            pred = pred_store.store(detection, baseline_ready=True)

        assert pred is not None
        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT * FROM pattern_history WHERE prediction_id = ?",
            (pred.prediction_id,),
        )
        assert len(rows) == 1
        assert rows[0]["outcome"] == "pending"
        assert rows[0]["pattern_type"] == "trend_drift"
        assert rows[0]["service"] == "api-gateway"
        assert abs(rows[0]["confidence"] - 0.72) < 0.001

    def test_record_outcome_writes_true_positive(self, tmp_path):
        pred_store, ops = self._make_store_with_ops(tmp_path)
        detection = FakeDetection(service="payment-svc")

        with patch("database.ops_persistence.get_ops_store", return_value=ops), \
             patch("database.persistence.get_engine", return_value=None), \
             patch("supervisor.confidence_calibrator.get_calibrator") as mock_cal, \
             patch("supervisor.confidence_calibrator._calibrator_lock"):
            mock_cal.return_value = MagicMock()
            pred = pred_store.store(detection, baseline_ready=True)
            pred_store.record_outcome("payment-svc", incident_id="INC-99")

        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT outcome FROM pattern_history WHERE prediction_id = ? ORDER BY rowid",
            (pred.prediction_id,),
        )
        outcomes = [r["outcome"] for r in rows]
        assert "pending" in outcomes
        assert "true_positive" in outcomes

    def test_mark_false_positive_writes_false_positive(self, tmp_path):
        pred_store, ops = self._make_store_with_ops(tmp_path)
        detection = FakeDetection(service="cache-svc")

        with patch("database.ops_persistence.get_ops_store", return_value=ops), \
             patch("database.persistence.get_engine", return_value=None), \
             patch("supervisor.confidence_calibrator.get_calibrator") as mock_cal, \
             patch("supervisor.confidence_calibrator._calibrator_lock"):
            mock_cal.return_value = MagicMock()
            pred = pred_store.store(detection, baseline_ready=True)
            pred_store.mark_false_positive(pred.prediction_id, reason="engineer confirmed noise")

        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT outcome FROM pattern_history WHERE prediction_id = ?",
            (pred.prediction_id,),
        )
        outcomes = [r["outcome"] for r in rows]
        assert "false_positive" in outcomes

    def test_expire_old_predictions_writes_expired(self, tmp_path):
        pred_store, ops = self._make_store_with_ops(tmp_path)
        # Create a prediction with a very short breach window so it's immediately expired
        detection = FakeDetection(
            service="db-svc",
            predicted_breach_hours=0.0001,  # essentially immediate expiry
        )

        with patch("database.ops_persistence.get_ops_store", return_value=ops), \
             patch("database.persistence.get_engine", return_value=None), \
             patch("supervisor.confidence_calibrator.get_calibrator") as mock_cal, \
             patch("supervisor.confidence_calibrator._calibrator_lock"):
            mock_cal.return_value = MagicMock()
            pred = pred_store.store(detection, baseline_ready=True)
            # Force expiry by making created_at old
            pred.created_at_epoch = time.time() - 9999
            pred_store.expire_old_predictions()

        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT outcome FROM pattern_history WHERE prediction_id = ?",
            (pred.prediction_id,),
        )
        outcomes = [r["outcome"] for r in rows]
        assert "expired" in outcomes

    def test_suppressed_prediction_writes_no_pending_event(self, tmp_path):
        """Duplicate-suppressed predictions should not write pattern_history rows."""
        pred_store, ops = self._make_store_with_ops(tmp_path)
        detection = FakeDetection(service="dedup-svc")

        with patch("database.ops_persistence.get_ops_store", return_value=ops), \
             patch("database.persistence.get_engine", return_value=None):
            pred_store.store(detection, baseline_ready=True)   # published
            suppressed = pred_store.store(detection, baseline_ready=True)  # deduped

        _flush(ops)
        assert suppressed is None  # confirm suppressed

        rows = _direct_query(
            ops._db_path,
            "SELECT COUNT(*) AS n FROM pattern_history WHERE service = 'dedup-svc'",
        )
        # Only the first (published) prediction should be recorded
        assert rows[0]["n"] == 1


# ── Convergence history ────────────────────────────────────────────────────────

class TestConvergenceHistory:
    def test_calibrator_update_writes_snapshot(self, tmp_path):
        from supervisor.confidence_calibrator import ConfidenceCalibrator
        ops = _start_store(tmp_path)

        cal = ConfidenceCalibrator()
        eval_results = [
            {"predicted_confidence": 75, "actual_correct": True},
            {"predicted_confidence": 80, "actual_correct": False},
            {"predicted_confidence": 60, "actual_correct": True},
        ]
        with patch("database.ops_persistence.get_ops_store", return_value=ops):
            cal.update(eval_results)

        _flush(ops)

        rows = _direct_query(
            ops._db_path, "SELECT * FROM convergence_history ORDER BY ts DESC LIMIT 1"
        )
        assert len(rows) == 1
        assert rows[0]["total_samples"] == 3
        assert 0.0 <= rows[0]["ece"] <= 1.0

    def test_convergence_history_loads_correctly(self, tmp_path):
        from supervisor.confidence_calibrator import ConfidenceCalibrator
        ops = _start_store(tmp_path)

        cal = ConfidenceCalibrator()
        for _ in range(3):
            with patch("database.ops_persistence.get_ops_store", return_value=ops):
                cal.update([{"predicted_confidence": 70, "actual_correct": True}])

        _flush(ops)

        history = ops.load_convergence_history(limit=10)
        assert len(history) == 3
        for snap in history:
            assert "ece" in snap
            assert "total_samples" in snap


# ── Weight history ─────────────────────────────────────────────────────────────

class TestWeightHistory:
    def test_weight_change_persisted(self, tmp_path):
        ops = _start_store(tmp_path)
        ops.persist_weight_change(
            incident_type="cpu_spike",
            step_label="log_worker.fetch_logs",
            weight_before=0.50,
            weight_after=0.58,
            quality_signal=0.82,
            calls=12,
            service="api-gateway",
        )
        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT * FROM weight_history WHERE step_label = ?",
            ("log_worker.fetch_logs",),
        )
        assert len(rows) == 1
        assert rows[0]["incident_type"] == "cpu_spike"
        assert abs(rows[0]["weight_before"] - 0.50) < 0.001
        assert abs(rows[0]["weight_after"] - 0.58) < 0.001
        assert rows[0]["calls"] == 12


# ── Safety events ──────────────────────────────────────────────────────────────

class TestSafetyEvents:
    def test_threshold_damped_event_persisted(self, tmp_path):
        ops = _start_store(tmp_path)
        ops.persist_safety_event(
            event_type="threshold_damped",
            threshold_name="critique_threshold",
            old_value=0.78,
            new_value=0.72,
            drift_fraction=0.45,
            context="auto_damp_drift",
        )
        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT * FROM safety_events WHERE event_type = 'threshold_damped'",
        )
        assert len(rows) == 1
        assert rows[0]["threshold_name"] == "critique_threshold"
        assert abs(rows[0]["old_value"] - 0.78) < 0.001

    def test_circuit_breaker_event_persisted(self, tmp_path):
        ops = _start_store(tmp_path)
        ops.persist_safety_event(
            event_type="circuit_breaker_fired",
            context="strategy_evolver",
            details={"step": "log_worker.fetch_logs", "consecutive_degradations": 3},
        )
        _flush(ops)

        rows = ops.load_recent_safety_events(limit=5)
        assert any(r["event_type"] == "circuit_breaker_fired" for r in rows)

    def test_adaptive_thresholds_auto_damp_writes_safety_event(self, tmp_path):
        """Integration: auto_damp_drift() → safety_events row via ops store."""
        import json, os
        ops = _start_store(tmp_path)

        # Create a threshold file with extreme drift
        thresh_path = str(tmp_path / "adaptive_thresholds.json")
        data = {
            "critique_threshold": {
                "name": "critique_threshold",
                "value": 0.79,   # hi bound is 0.80, default 0.62 → drift = (0.79-0.62)/(0.80-0.40) = 0.425 > 0.50?
                # Actually (0.79-0.62)/0.40 = 0.425 -- need > 0.50 for damp
                # Let's use 0.82 to exceed bounds -- but bounds cap at 0.80
                # Use value=0.80 (max), default=0.62, range=0.40, drift=(0.80-0.62)/0.40=0.45
                # Still < 0.50. Need drift > 0.50, so value = 0.62 + 0.50*0.40 + 0.01 = 0.83 → clipped to 0.80
                # Adjust: use store_quality_threshold: hi=0.85, lo=0.40, default=0.60, range=0.45
                # drift = (0.85-0.60)/0.45 = 0.556 → > 0.50 ✓
            }
        }
        # Rebuild with a drifted threshold that exceeds 50% of range
        data = {
            "store_quality_threshold": {
                "name": "store_quality_threshold",
                "value": 0.85,    # hi bound
                "default": 0.60,
                "observations": 50,
                "ema_signal": 0.75,
                "last_updated": "2026-01-01T00:00:00+00:00",
            }
        }
        with open(thresh_path, "w") as f:
            json.dump(data, f)

        with patch("supervisor.adaptive_thresholds.ADAPTIVE_THRESHOLDS_PATH", thresh_path), \
             patch("database.ops_persistence.get_ops_store", return_value=ops):
            from supervisor.adaptive_thresholds import auto_damp_drift
            actions = auto_damp_drift()

        _flush(ops)

        rows = _direct_query(
            ops._db_path,
            "SELECT * FROM safety_events WHERE event_type = 'threshold_damped'",
        )
        assert len(rows) >= 1, f"Expected safety event for damped threshold; actions={actions}"


# ── KV state ───────────────────────────────────────────────────────────────────

class TestKVState:
    def test_set_and_get_string(self, tmp_path):
        store = _start_store(tmp_path)
        store.set_state("warming_api_gateway", True)
        _flush(store)
        val = store.get_state("warming_api_gateway")
        assert val is True

    def test_overwrite_key(self, tmp_path):
        store = _start_store(tmp_path)
        store.set_state("counter", 1)
        store.set_state("counter", 2)
        _flush(store)
        assert store.get_state("counter") == 2

    def test_missing_key_returns_default(self, tmp_path):
        store = _start_store(tmp_path)
        _flush(store)
        assert store.get_state("nonexistent_key", default="fallback") == "fallback"

    def test_complex_value_roundtrip(self, tmp_path):
        store = _start_store(tmp_path)
        payload = {"service": "api", "warming": True, "count": 42, "scores": [0.1, 0.2]}
        store.set_state("intel_warming_state", payload)
        _flush(store)
        result = store.get_state("intel_warming_state")
        assert result == payload


# ── Restart survival ───────────────────────────────────────────────────────────

class TestRestartSurvival:
    """Simulate process restart: stop instance 1, open fresh instance on same DB, verify data."""

    def test_receipts_survive_restart(self, tmp_path):
        # Phase 1: write
        store1 = _start_store(tmp_path)
        inv_id = "inv-restart-001"
        for i in range(3):
            r = _make_receipt(inv_id)
            r.action = f"step_{i}"
            store1.persist_receipt(r)
        _flush(store1)  # stop + flush

        # Phase 2: new instance, same DB
        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()

        rows = store2.load_receipts_for_investigation(inv_id)
        store2.stop()

        assert len(rows) == 3
        actions = {r["action"] for r in rows}
        assert actions == {"step_0", "step_1", "step_2"}

    def test_pattern_history_survives_restart(self, tmp_path):
        store1 = _start_store(tmp_path)
        store1.persist_pattern_event(
            pattern_type="rate_accel",
            prediction_id="pred-999",
            service="worker-svc",
            outcome="true_positive",
            confidence=0.88,
        )
        _flush(store1)

        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()
        rows = _direct_query(
            store2._db_path,
            "SELECT * FROM pattern_history WHERE prediction_id = 'pred-999'",
        )
        store2.stop()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "true_positive"

    def test_convergence_history_survives_restart(self, tmp_path):
        store1 = _start_store(tmp_path)
        store1.persist_convergence_snapshot(
            investigation_id="", ece=0.08, total_samples=120,
            mean_confidence=0.73, bins_with_data=7,
        )
        _flush(store1)

        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()
        history = store2.load_convergence_history(limit=5)
        store2.stop()

        assert len(history) == 1
        assert history[0]["ece"] == pytest.approx(0.08)
        assert history[0]["total_samples"] == 120

    def test_safety_events_survive_restart(self, tmp_path):
        store1 = _start_store(tmp_path)
        store1.persist_safety_event(
            event_type="threshold_damped",
            threshold_name="skip_weight_threshold",
            old_value=0.52,
            new_value=0.48,
            drift_fraction=0.51,
        )
        _flush(store1)

        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()
        events = store2.load_recent_safety_events(limit=10)
        store2.stop()

        assert len(events) == 1
        assert events[0]["event_type"] == "threshold_damped"

    def test_kv_state_survives_restart(self, tmp_path):
        store1 = _start_store(tmp_path)
        store1.set_state("service_warming_state", {"api": True, "cache": False})
        _flush(store1)

        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()
        val = store2.get_state("service_warming_state")
        store2.stop()

        assert val == {"api": True, "cache": False}

    def test_all_tables_populated_survive_restart(self, tmp_path):
        """Write to all 6 write-target tables, restart, verify all rows present."""
        store1 = _start_store(tmp_path)

        store1.persist_receipt(_make_receipt("inv-all-001"))
        store1.persist_weight_change("cpu_spike", "metrics_worker.get", 0.4, 0.5, 0.7, 5)
        store1.persist_convergence_snapshot("", 0.10, 50, 0.65, 4)
        store1.persist_pattern_event("trend_drift", "pred-all-001", "svc-a", "pending", 0.75)
        store1.persist_safety_event("threshold_damped", "critique_threshold", 0.79, 0.75, 0.42)
        store1.persist_replay_meta({
            "investigation_id": "inv-all-001",
            "outcome": "resolved",
            "critique_score": 0.88,
            "overall_quality": 0.82,
            "refinement_triggered": False,
            "refinement_helped": False,
            "confidence_before": 50.0,
            "confidence_after": 75.0,
            "gap_patterns": [],
            "step_weights_used": {},
            "corrections": [],
        })
        store1.set_state("test_key", "test_val")
        _flush(store1)

        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()

        health = store2.get_health()
        store2.stop()

        rc = health["row_counts"]
        assert rc["enriched_receipts"] >= 1, "receipts not persisted"
        assert rc["weight_history"] >= 1, "weight_history not persisted"
        assert rc["convergence_history"] >= 1, "convergence_history not persisted"
        assert rc["pattern_history"] >= 1, "pattern_history not persisted"
        assert rc["safety_events"] >= 1, "safety_events not persisted"
        assert rc["replay_meta"] >= 1, "replay_meta not persisted"


# ── Receipt recovery (transparency layer) ─────────────────────────────────────

class TestReceiptRecoveryAfterRestart:
    """ToolTransparencyEmitter.get_receipts() must reload from DB on fresh emitter."""

    def test_get_receipts_reloads_from_db_after_restart(self, tmp_path):
        inv_id = "inv-recovery-001"

        # Phase 1: write via store1
        store1 = _start_store(tmp_path)
        receipt = _make_receipt(inv_id)
        store1.persist_receipt(receipt)
        _flush(store1)

        # Phase 2: fresh emitter (empty in-memory), loads from store2 on same DB
        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()

        emitter = ToolTransparencyEmitter()  # fresh, no in-memory receipts
        with patch("database.ops_persistence.get_ops_store", return_value=store2):
            recovered = emitter.get_receipts(inv_id)
        store2.stop()

        assert len(recovered) >= 1
        found = next((r for r in recovered if r.receipt_id == receipt.receipt_id), None)
        assert found is not None, "Original receipt not recovered from DB"
        assert found.worker == "log_worker"
        assert found.phase == "collect"

    def test_in_memory_takes_precedence_over_db(self, tmp_path):
        """If emitter already has in-memory receipts, it must not reload from DB."""
        inv_id = "inv-precedence-001"

        store1 = _start_store(tmp_path)
        db_receipt = _make_receipt(inv_id)
        db_receipt.action = "from_db"
        store1.persist_receipt(db_receipt)
        _flush(store1)

        store2 = OpsPersistence(db_path=store1._db_path)
        store2.start()

        emitter = ToolTransparencyEmitter()
        # Populate in-memory with a different receipt
        mem_receipt = _make_receipt(inv_id)
        mem_receipt.action = "from_memory"
        emitter._store(inv_id).add(mem_receipt)

        with patch("database.ops_persistence.get_ops_store", return_value=store2):
            receipts = emitter.get_receipts(inv_id)
        store2.stop()

        actions = {r.action for r in receipts}
        assert "from_memory" in actions
        assert "from_db" not in actions, "DB should not override in-memory receipts"


# ── Replay consistency ─────────────────────────────────────────────────────────

class TestReplayConsistency:
    """Run the full prediction lifecycle twice, verify row counts are deterministic."""

    def _run_lifecycle(self, pred_store, ops, service_suffix: str) -> dict:
        """One full lifecycle: store → record_outcome → mark_false_positive → expire."""
        svc_a = f"svc-tp-{service_suffix}"
        svc_b = f"svc-fp-{service_suffix}"
        svc_c = f"svc-ex-{service_suffix}"

        det_a = FakeDetection(service=svc_a, confidence=0.80)
        det_b = FakeDetection(service=svc_b, confidence=0.65, pattern_type="rate_accel")
        det_c = FakeDetection(service=svc_c, confidence=0.55, predicted_breach_hours=0.0001)

        with patch("database.ops_persistence.get_ops_store", return_value=ops), \
             patch("database.persistence.get_engine", return_value=None), \
             patch("supervisor.confidence_calibrator.get_calibrator") as mc, \
             patch("supervisor.confidence_calibrator._calibrator_lock"):
            mc.return_value = MagicMock()

            p_a = pred_store.store(det_a, baseline_ready=True)
            p_b = pred_store.store(det_b, baseline_ready=True)
            p_c = pred_store.store(det_c, baseline_ready=True)

            pred_store.record_outcome(svc_a, incident_id="INC-TP")
            pred_store.mark_false_positive(p_b.prediction_id, "noise")
            # Force p_c to be expired
            p_c.created_at_epoch = time.time() - 9999
            pred_store.expire_old_predictions()

        return {
            "p_a": p_a.prediction_id,
            "p_b": p_b.prediction_id,
            "p_c": p_c.prediction_id,
        }

    def test_two_lifecycles_produce_deterministic_row_counts(self, tmp_path):
        from intelligence.prediction_store import PredictionStore

        ops = _start_store(tmp_path)
        pred_store1 = PredictionStore()
        pred_store2 = PredictionStore()

        ids1 = self._run_lifecycle(pred_store1, ops, "run1")
        ids2 = self._run_lifecycle(pred_store2, ops, "run2")
        _flush(ops)

        # Each lifecycle produces exactly 2 rows per prediction (pending + outcome)
        # except for the expired one which also gets pending + expired
        for label, pred_id in {**ids1, **ids2}.items():
            rows = _direct_query(
                ops._db_path,
                "SELECT outcome FROM pattern_history WHERE prediction_id = ? ORDER BY rowid",
                (pred_id,),
            )
            assert len(rows) == 2, (
                f"Expected 2 pattern_history rows for {label} ({pred_id}), got {len(rows)}: {rows}"
            )
            assert rows[0]["outcome"] == "pending"

    def test_two_lifecycles_correct_final_outcomes(self, tmp_path):
        from intelligence.prediction_store import PredictionStore

        ops = _start_store(tmp_path)

        # Run both lifecycles before flushing; stop() drains the queue once.
        all_ids = {}
        for run in ("run3", "run4"):
            pred_store = PredictionStore()
            all_ids[run] = self._run_lifecycle(pred_store, ops, run)

        _flush(ops)  # single drain after both runs

        for run, ids in all_ids.items():
            # p_a → true_positive
            rows = _direct_query(
                ops._db_path,
                "SELECT outcome FROM pattern_history WHERE prediction_id = ? ORDER BY rowid",
                (ids["p_a"],),
            )
            assert rows and rows[-1]["outcome"] == "true_positive", (
                f"run={run} p_a final wrong: {rows}"
            )

            # p_b → false_positive
            rows = _direct_query(
                ops._db_path,
                "SELECT outcome FROM pattern_history WHERE prediction_id = ? ORDER BY rowid",
                (ids["p_b"],),
            )
            assert rows and rows[-1]["outcome"] == "false_positive", (
                f"run={run} p_b final wrong: {rows}"
            )

            # p_c → expired
            rows = _direct_query(
                ops._db_path,
                "SELECT outcome FROM pattern_history WHERE prediction_id = ? ORDER BY rowid",
                (ids["p_c"],),
            )
            assert rows and rows[-1]["outcome"] == "expired", (
                f"run={run} p_c final wrong: {rows}"
            )


# ── Health report ──────────────────────────────────────────────────────────────

class TestHealthReport:
    def test_health_reports_correct_row_counts(self, tmp_path):
        store = _start_store(tmp_path)

        store.persist_receipt(_make_receipt("inv-h1"))
        store.persist_receipt(_make_receipt("inv-h1"))
        store.persist_weight_change("t", "s", 0.3, 0.4, 0.6, 1)
        store.persist_pattern_event("trend_drift", "p1", "svc", "pending", 0.7)
        _flush(store)

        store2 = OpsPersistence(db_path=store._db_path)
        store2.start()
        health = store2.get_health()
        store2.stop()

        assert health["enabled"] is True
        assert health["ready"] is True
        rc = health["row_counts"]
        assert rc["enriched_receipts"] == 2
        assert rc["weight_history"] == 1
        assert rc["pattern_history"] == 1

    def test_health_reports_queue_depth(self, tmp_path):
        store = _start_store(tmp_path)
        # Do not flush — check queue is non-empty before drain
        store.persist_receipt(_make_receipt("inv-q1"))
        # Queue depth may be 0 if writer is fast; just verify key exists
        health = store.get_health()
        assert "queue_depth" in health
        assert isinstance(health["queue_depth"], int)
        _flush(store)


# ── Corruption guard ───────────────────────────────────────────────────────────

class TestCorruptionGuard:
    def test_corrupt_db_is_quarantined_and_recreated(self, tmp_path):
        db_path = str(tmp_path / "corrupt_ops.db")
        # Write garbage to the DB file
        with open(db_path, "wb") as f:
            f.write(b"GARBAGE_NOT_SQLITE_DATA" * 100)

        store = OpsPersistence(db_path=db_path)
        store.start()  # should quarantine + recreate
        assert store._ready is True
        _flush(store)

        # Quarantine file exists
        quarantined = [
            f for f in os.listdir(tmp_path)
            if "corrupt" in f and f != "corrupt_ops.db"
        ]
        assert len(quarantined) >= 1, "No quarantine file found after corrupt DB"

        # Fresh DB is intact
        rows = _direct_query(
            db_path,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_history'",
        )
        assert len(rows) == 1


# ── Retention cleanup ──────────────────────────────────────────────────────────

class TestRetentionCleanup:
    def test_old_pattern_events_removed_on_startup(self, tmp_path):
        """Rows older than retention window are deleted when a new instance starts."""
        db_path = str(tmp_path / "retention_ops.db")

        # Write an old row directly
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pattern_history (
                ts REAL, pattern_type TEXT, prediction_id TEXT,
                service TEXT, outcome TEXT, confidence REAL
            )
        """)
        old_ts = time.time() - (91 * 86400)   # 91 days ago (> 90-day window)
        conn.execute(
            "INSERT INTO pattern_history VALUES (?,?,?,?,?,?)",
            (old_ts, "trend_drift", "pred-old", "svc", "pending", 0.5),
        )
        conn.commit()
        conn.close()

        # Start a fresh store — retention cleanup runs on startup
        store = OpsPersistence(db_path=db_path)
        store.start()
        _flush(store)

        rows = _direct_query(
            db_path,
            "SELECT * FROM pattern_history WHERE prediction_id = 'pred-old'",
        )
        assert len(rows) == 0, "Old row should have been deleted by retention cleanup"


# ── Queue behaviour ────────────────────────────────────────────────────────────

class TestQueueBehaviour:
    def test_items_in_queue_flushed_on_stop(self, tmp_path):
        """stop() must flush all queued writes before returning."""
        store = _start_store(tmp_path)

        receipts = [_make_receipt(f"inv-q-{i}") for i in range(20)]
        for r in receipts:
            store.persist_receipt(r)

        store.stop()  # poison pill → flush → thread exits

        total = _direct_query(
            store._db_path, "SELECT COUNT(*) AS n FROM enriched_receipts"
        )
        assert total[0]["n"] == 20

    def test_queue_full_does_not_raise(self, tmp_path):
        """When queue is full, _enqueue silently drops — never raises."""
        store = _start_store(tmp_path)
        # Temporarily shrink queue to force overflow
        import queue as _queue
        store._q = _queue.Queue(maxsize=2)

        for _ in range(10):
            # Must not raise
            store.persist_pattern_event("trend_drift", uuid.uuid4().hex, "svc", "pending", 0.5)

        store.stop()  # no assertion — just verify no exception raised
