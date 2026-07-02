"""Phase 16 — phase execution receipts tests.

Verifies:
- PhaseExecutionReceipt is JSON-safe (round-trip via to_dict/from_dict)
- PhaseReceiptCollector context manager records timing + status
- Exceptions inside a record() block are re-raised, error_type captured
- Missing evidence counts don't crash
- Receipts do not alter the returned result's semantic fields
- Replay path is unaffected (does not add receipts)
- Attach helper is a no-op on non-dict results
- status_from_result maps PhaseStatus values mechanically
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from sentinel_core.models.phase_receipt import (
    PhaseExecutionReceipt,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    normalize_status,
)
from sentinel_core.models.workflow import PhaseStatus
from supervisor.phase_receipts import (
    PhaseReceiptCollector,
    RECEIPTS_RESULT_KEY,
    attach_receipts,
    map_phase_status,
    status_from_result,
)


# ---------------------------------------------------------------------------
# Model — JSON safety
# ---------------------------------------------------------------------------

class TestReceiptModel:
    def test_defaults_are_json_safe(self):
        r = PhaseExecutionReceipt(phase_name="fetch")
        json.dumps(r.to_dict())  # must not raise

    def test_full_roundtrip(self):
        original = PhaseExecutionReceipt(
            phase_name="collect",
            status=STATUS_DEGRADED,
            started_at=100.0, completed_at=105.5, elapsed_ms=5500.0,
            evidence_count_before=0, evidence_count_after=12,
            warnings=("gate:collection near threshold",),
            degraded_reason="one worker unavailable",
            error_type="",
            metadata={"note": "playbook truncated"},
        )
        d = original.to_dict()
        # JSON round-trip
        restored = PhaseExecutionReceipt.from_dict(json.loads(json.dumps(d)))
        assert restored == original

    def test_from_dict_tolerates_missing_fields(self):
        r = PhaseExecutionReceipt.from_dict({"phase_name": "fetch"})
        assert r.phase_name == "fetch"
        assert r.status == STATUS_SUCCESS
        assert r.warnings == ()
        assert r.metadata == {}

    def test_from_dict_tolerates_malformed_types(self):
        # warnings not a list, metadata not a dict — both must be normalised
        r = PhaseExecutionReceipt.from_dict({
            "phase_name": "x",
            "warnings":   "not a list",
            "metadata":   ["not a dict"],
            "started_at": None,
            "elapsed_ms": None,
        })
        assert r.warnings == ()
        assert r.metadata == {}
        assert r.started_at == 0.0
        assert r.elapsed_ms == 0.0

    def test_normalize_status_unknown_falls_back_to_success(self):
        assert normalize_status("bogus") == STATUS_SUCCESS
        assert normalize_status("")      == STATUS_SUCCESS
        assert normalize_status("SUCCESS") == STATUS_SUCCESS

    def test_normalize_status_preserves_known_values(self):
        for s in (STATUS_SUCCESS, STATUS_DEGRADED, STATUS_SKIPPED, STATUS_FAILED):
            assert normalize_status(s) == s


# ---------------------------------------------------------------------------
# Status mapping (mechanical)
# ---------------------------------------------------------------------------

class TestStatusMapping:
    def test_completed_maps_to_success(self):
        assert map_phase_status(PhaseStatus.COMPLETED) == STATUS_SUCCESS

    def test_failed_maps_to_failed(self):
        assert map_phase_status(PhaseStatus.FAILED) == STATUS_FAILED

    def test_skipped_maps_to_skipped(self):
        assert map_phase_status(PhaseStatus.SKIPPED) == STATUS_SKIPPED

    def test_pending_maps_to_skipped(self):
        assert map_phase_status(PhaseStatus.PENDING) == STATUS_SKIPPED

    def test_status_from_result_with_phase_result_shape(self):
        obj = SimpleNamespace(status=PhaseStatus.COMPLETED)
        assert status_from_result(obj) == STATUS_SUCCESS

    def test_status_from_result_with_failed_phase(self):
        obj = SimpleNamespace(status=PhaseStatus.FAILED)
        assert status_from_result(obj) == STATUS_FAILED

    def test_status_from_result_with_none(self):
        assert status_from_result(None) == STATUS_SUCCESS

    def test_status_from_result_never_raises(self):
        # Even on truly odd input, we get a valid status back
        class _Boom:
            @property
            def status(self):
                raise RuntimeError("boom")
        assert status_from_result(_Boom()) == STATUS_SUCCESS


# ---------------------------------------------------------------------------
# Collector — recording behavior
# ---------------------------------------------------------------------------

class TestCollectorRecord:
    def test_records_a_receipt_per_phase(self):
        c = PhaseReceiptCollector()
        with c.record("fetch") as r:
            r.status = STATUS_SUCCESS
        with c.record("classify") as r:
            r.status = STATUS_SUCCESS
        assert len(c) == 2
        assert [r.phase_name for r in c.receipts()] == ["fetch", "classify"]

    def test_captures_elapsed_ms(self):
        c = PhaseReceiptCollector()
        with c.record("fetch"):
            time.sleep(0.01)  # 10ms
        rec = c.receipts()[0]
        assert rec.elapsed_ms >= 5.0  # allow for OS jitter
        assert rec.completed_at >= rec.started_at

    def test_preserves_phase_order(self):
        c = PhaseReceiptCollector()
        for name in ("fetch", "classify", "collect", "analyze", "persist"):
            with c.record(name):
                pass
        assert [r.phase_name for r in c.receipts()] == [
            "fetch", "classify", "collect", "analyze", "persist",
        ]

    def test_status_defaults_to_success(self):
        c = PhaseReceiptCollector()
        with c.record("fetch"):
            pass
        assert c.receipts()[0].status == STATUS_SUCCESS

    def test_caller_can_override_status(self):
        c = PhaseReceiptCollector()
        with c.record("fetch") as r:
            r.status = STATUS_DEGRADED
            r.degraded_reason = "worker missing"
        rec = c.receipts()[0]
        assert rec.status == STATUS_DEGRADED
        assert rec.degraded_reason == "worker missing"

    def test_evidence_counts_captured(self):
        c = PhaseReceiptCollector()
        with c.record("collect", evidence_before=0) as r:
            r.evidence_after = 12
        rec = c.receipts()[0]
        assert rec.evidence_count_before == 0
        assert rec.evidence_count_after == 12

    def test_metadata_bag_persists(self):
        c = PhaseReceiptCollector()
        with c.record("classify") as r:
            r.metadata["confluence_hit"] = True
        assert c.receipts()[0].metadata == {"confluence_hit": True}


# ---------------------------------------------------------------------------
# Collector — exception handling
# ---------------------------------------------------------------------------

class TestCollectorExceptions:
    def test_exception_recorded_and_re_raised(self):
        c = PhaseReceiptCollector()
        with pytest.raises(RuntimeError, match="upstream failure"):
            with c.record("fetch") as r:
                r.status = STATUS_SUCCESS  # will be overridden by the failure path
                raise RuntimeError("upstream failure")
        rec = c.receipts()[0]
        assert rec.status == STATUS_FAILED
        assert rec.error_type == "RuntimeError"

    def test_original_exception_class_preserved(self):
        c = PhaseReceiptCollector()
        with pytest.raises(ValueError):
            with c.record("classify"):
                raise ValueError("bad input")
        assert c.receipts()[0].error_type == "ValueError"

    def test_exception_still_produces_a_receipt(self):
        c = PhaseReceiptCollector()
        try:
            with c.record("collect"):
                raise KeyError("k")
        except KeyError:
            pass
        assert len(c.receipts()) == 1
        assert c.receipts()[0].phase_name == "collect"


# ---------------------------------------------------------------------------
# Collector — to_list JSON safety
# ---------------------------------------------------------------------------

class TestCollectorToList:
    def test_empty_collector_produces_empty_list(self):
        c = PhaseReceiptCollector()
        assert c.to_list() == []

    def test_to_list_is_json_serialisable(self):
        c = PhaseReceiptCollector()
        with c.record("fetch") as r:
            r.metadata["k"] = "v"
        with c.record("classify"):
            pass
        json.dumps(c.to_list())  # must not raise

    def test_to_list_preserves_order(self):
        c = PhaseReceiptCollector()
        for name in ("a", "b", "c"):
            with c.record(name):
                pass
        assert [d["phase_name"] for d in c.to_list()] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# attach_receipts helper
# ---------------------------------------------------------------------------

class TestAttachReceipts:
    def test_attaches_to_result_dict(self):
        c = PhaseReceiptCollector()
        with c.record("fetch"):
            pass
        result = {"root_cause": "x"}
        attach_receipts(result, c)
        assert RECEIPTS_RESULT_KEY in result
        assert result[RECEIPTS_RESULT_KEY][0]["phase_name"] == "fetch"

    def test_does_not_change_other_result_fields(self):
        c = PhaseReceiptCollector()
        with c.record("fetch"):
            pass
        result = {"root_cause": "x", "confidence": 70, "reasoning": "y"}
        attach_receipts(result, c)
        assert result["root_cause"] == "x"
        assert result["confidence"] == 70
        assert result["reasoning"] == "y"

    def test_returns_same_reference(self):
        c = PhaseReceiptCollector()
        result = {}
        assert attach_receipts(result, c) is result

    def test_no_op_on_non_dict(self):
        c = PhaseReceiptCollector()
        # Should not raise, just return the value untouched
        assert attach_receipts(None, c) is None
        assert attach_receipts("not a dict", c) == "not a dict"

    def test_no_op_on_none_collector(self):
        result = {"x": 1}
        attach_receipts(result, None)
        assert RECEIPTS_RESULT_KEY not in result

    def test_attach_key_is_underscore_prefixed(self):
        """Matches the internal-metadata convention (_evidence_snapshot,
        _gate_post_collection, _llm_metrics, ...)."""
        assert RECEIPTS_RESULT_KEY.startswith("_")


# ---------------------------------------------------------------------------
# End-to-end via investigate() — receipts appear on the returned result
# ---------------------------------------------------------------------------

class TestEndToEndAttachment:
    """Verify that a real investigate() call produces phase receipts attached
    under the underscore key AND that the pre-existing result fields are
    untouched. Uses the same mock-workers pattern as test_analyzer_branches."""

    def _make_supervisor(self):
        from unittest.mock import MagicMock, Mock, patch
        from supervisor.agent import SentinalAISupervisor

        supervisor = SentinalAISupervisor()

        def mock_ops(action, params):
            if action == "get_incident_by_id":
                return {"incident": {
                    "incident_id": "INC_R1",
                    "summary": "checkout 5xx spike",
                    "affected_service": "checkout",
                }}
            return {}

        for name in supervisor.workers:
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(side_effect=lambda a, p: {})
        supervisor.workers["ops_worker"].execute = Mock(side_effect=mock_ops)
        supervisor.workers["knowledge_worker"].execute = Mock(
            side_effect=lambda a, p: {"similar_incidents": []},
        )
        return supervisor

    def test_receipts_attached_to_result(self):
        from unittest.mock import patch
        supervisor = self._make_supervisor()
        # Suppress hypothesis priming for a deterministic run
        with patch("supervisor.agent._retrieve_experiences", return_value=[]), \
             patch("supervisor.agent._get_tool_recommendations", return_value={}), \
             patch("supervisor.agent._kg_query_similar", return_value=[]), \
             patch("supervisor.experience_store.retrieve_similar", return_value=[]), \
             patch("supervisor.experience_store.get_tool_recommendations", return_value={}), \
             patch("supervisor.knowledge_graph.query_similar", return_value=[]):
            result = supervisor.investigate("INC_R1")

        assert RECEIPTS_RESULT_KEY in result
        receipts = result[RECEIPTS_RESULT_KEY]
        assert isinstance(receipts, list)
        # We recorded one per phase; the exact phase list depends on early-
        # returns but for a normal run we get all five.
        names = [r["phase_name"] for r in receipts]
        assert "fetch" in names

    def test_existing_result_fields_still_present(self):
        from unittest.mock import patch
        supervisor = self._make_supervisor()
        with patch("supervisor.agent._retrieve_experiences", return_value=[]), \
             patch("supervisor.agent._get_tool_recommendations", return_value={}), \
             patch("supervisor.agent._kg_query_similar", return_value=[]), \
             patch("supervisor.experience_store.retrieve_similar", return_value=[]), \
             patch("supervisor.experience_store.get_tool_recommendations", return_value={}), \
             patch("supervisor.knowledge_graph.query_similar", return_value=[]):
            result = supervisor.investigate("INC_R1")
        # These are the public result keys documented in investigate()'s docstring
        for k in ("root_cause", "confidence"):
            assert k in result


# ---------------------------------------------------------------------------
# Replay path
# ---------------------------------------------------------------------------

class TestReplayPath:
    """The replay short-circuit runs BEFORE the phase-receipt collector is
    even instantiated, so replayed results must NOT gain a `_phase_receipts`
    key. This confirms the receipts wiring doesn't leak into replay."""

    def test_replay_result_has_no_receipts_key(self, tmp_path, monkeypatch):
        import json as _json
        from unittest.mock import MagicMock, Mock
        from supervisor.agent import SentinalAISupervisor
        from supervisor.replay import ReplayStore

        # Point replay store at a temp directory and seed one artifact
        store = ReplayStore(replay_dir=str(tmp_path))
        # ReplayStore matches files by the "{case_id}_*.json" glob pattern
        artifact = {
            "case_id": "INC_R2",
            "result": {"root_cause": "cached cause", "confidence": 88,
                        "evidence_timeline": [], "reasoning": "cached"},
            "evidence": {},  # empty -> falls through to `if stored.get("result")` branch
        }
        with open(tmp_path / "INC_R2_20260702T100000Z.json", "w") as fh:
            _json.dump(artifact, fh)

        supervisor = SentinalAISupervisor()
        supervisor._replay_store = store
        # Ensure workers exist for import-time safety even though replay
        # never reaches them
        for name in supervisor.workers:
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(side_effect=lambda a, p: {})

        result = supervisor.investigate("INC_R2", replay=True)
        # The replay fallback path returns the CACHED result — receipts must
        # not have been attached (they belong to the live-run path).
        assert RECEIPTS_RESULT_KEY not in result
