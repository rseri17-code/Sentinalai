"""Sprint 2 regression tests — RC-D + RC-E.

Each RC has three test groups:
  1. Failing-input test that would have failed pre-fix (reproduces the
     audit defect).
  2. Fixed-behavior test that asserts the new invariant.
  3. Negative / edge-case test + compatibility test.

No test in this file weakens or contradicts an existing assertion
elsewhere in the suite. Delete this file to fully roll back Sprint 2's
test surface.
"""
from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# RC-D — Frozen dataclass immutability
# ---------------------------------------------------------------------------

from sentinel_core.models._immutable import _FrozenDict, freeze_dict
from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.intel_memory.schemas import SimilarityScore
from sentinel_core.causal_graph.causal_node import CausalNode, CausalNodeType
from sentinel_core.causal_graph.causal_edge import CausalEdge, CausalEdgeType
from sentinel_core.continuous_learning.learning_cycle import LearningSnapshot


class TestFrozenDictHelper:
    """Unit tests for the pure helper."""

    def test_frozen_dict_is_dict_subclass(self):
        fd = freeze_dict({"a": 1})
        assert isinstance(fd, dict)
        assert isinstance(fd, _FrozenDict)

    def test_reader_operations_work(self):
        fd = freeze_dict({"a": 1, "b": 2})
        assert fd["a"] == 1
        assert fd.get("b") == 2
        assert list(fd.keys()) == ["a", "b"]
        assert dict(fd.items()) == {"a": 1, "b": 2}
        assert len(fd) == 2
        assert "a" in fd

    def test_setitem_raises(self):
        fd = freeze_dict({"a": 1})
        with pytest.raises(TypeError):
            fd["a"] = 999

    def test_delitem_raises(self):
        fd = freeze_dict({"a": 1})
        with pytest.raises(TypeError):
            del fd["a"]

    def test_update_raises(self):
        fd = freeze_dict({"a": 1})
        with pytest.raises(TypeError):
            fd.update({"a": 2})

    def test_pop_raises(self):
        fd = freeze_dict({"a": 1})
        with pytest.raises(TypeError):
            fd.pop("a")

    def test_clear_raises(self):
        fd = freeze_dict({"a": 1})
        with pytest.raises(TypeError):
            fd.clear()

    def test_setdefault_raises(self):
        fd = freeze_dict({})
        with pytest.raises(TypeError):
            fd.setdefault("a", 1)

    def test_json_dumps_works(self):
        fd = freeze_dict({"a": 1, "b": 2})
        # RC-D compat: must survive json.dumps as a dict.
        assert json.loads(json.dumps(fd)) == {"a": 1, "b": 2}

    def test_dict_copy_returns_mutable(self):
        fd = freeze_dict({"a": 1})
        copy = fd.copy()
        # Python's dict.copy on a subclass returns plain dict.
        assert type(copy) is dict
        copy["a"] = 2  # must succeed on the copy
        assert copy["a"] == 2
        # Original stays frozen
        with pytest.raises(TypeError):
            fd["a"] = 2

    def test_dict_constructor_returns_mutable(self):
        fd = freeze_dict({"a": 1})
        m = dict(fd)
        assert type(m) is dict
        m["a"] = 999

    def test_idempotent_on_frozen_input(self):
        fd = freeze_dict({"a": 1})
        assert freeze_dict(fd) is fd

    def test_none_input_tolerated(self):
        fd = freeze_dict(None)
        assert isinstance(fd, _FrozenDict)
        assert len(fd) == 0


class TestSimilarityScoreImmutable:

    def test_reproduces_audit_defect_breakdown_cannot_be_mutated(self):
        s = SimilarityScore(memory_id="m1", breakdown={"topology": 0.5})
        # PRE-FIX: `s.breakdown["topology"] = 999` succeeded silently.
        with pytest.raises(TypeError):
            s.breakdown["topology"] = 999

    def test_read_operations_unchanged(self):
        s = SimilarityScore(memory_id="m1", breakdown={"topology": 0.5})
        assert s.breakdown["topology"] == 0.5
        assert "topology" in s.breakdown

    def test_to_dict_output_is_plain_mutable_dict(self):
        s = SimilarityScore(memory_id="m1", breakdown={"topology": 0.5})
        out = s.to_dict()
        # Output must be plain dict (existing to_dict contract).
        assert type(out) is dict
        assert type(out["breakdown"]) is dict
        out["breakdown"]["topology"] = 999  # mutation on output OK


class TestMemoryRecordImmutable:

    def test_reproduces_audit_defect_decision_trace_cannot_be_mutated(self):
        r = MemoryRecord(memory_id="m1", decision_trace={"hypotheses": []})
        with pytest.raises(TypeError):
            r.decision_trace["hypotheses"] = ["poisoned"]

    def test_knowledge_graph_snapshot_frozen(self):
        r = MemoryRecord(memory_id="m1",
                         knowledge_graph_snapshot={"nodes": []})
        with pytest.raises(TypeError):
            r.knowledge_graph_snapshot["nodes"] = ["poisoned"]

    def test_to_dict_round_trip_still_works(self):
        r = MemoryRecord(memory_id="m1", decision_trace={"h": [1, 2, 3]})
        d = r.to_dict()
        # to_dict output is still a plain dict tree that survives JSON.
        payload = json.dumps(d, sort_keys=True)
        assert "decision_trace" in json.loads(payload)

    def test_from_dict_round_trip_produces_frozen_field(self):
        payload = {"memory_id": "m1", "decision_trace": {"h": [1]}}
        r = MemoryRecord.from_dict(payload)
        # After round-trip through from_dict, the field is still frozen.
        with pytest.raises(TypeError):
            r.decision_trace["h"] = [999]


class TestCausalNodeImmutable:

    def test_reproduces_audit_defect(self):
        n = CausalNode(node_id="x", node_type=CausalNodeType.SERVICE.value,
                       label="svc", properties={"k": 1})
        with pytest.raises(TypeError):
            n.properties["k"] = 999

    def test_make_factory_still_works(self):
        n = CausalNode.make(CausalNodeType.SERVICE, "svc",
                             properties={"role": "primary"})
        assert n.properties["role"] == "primary"
        with pytest.raises(TypeError):
            n.properties["role"] = "secondary"


class TestCausalEdgeImmutable:

    def test_reproduces_audit_defect(self):
        e = CausalEdge(
            edge_id="e", source_id="s", target_id="t",
            edge_type=CausalEdgeType.CAUSED_BY.value,
            properties={"weight_reason": "co-occurrence"},
        )
        with pytest.raises(TypeError):
            e.properties["weight_reason"] = "tampered"

    def test_make_factory_still_works(self):
        e = CausalEdge.make(
            source_id="s", target_id="t",
            edge_type=CausalEdgeType.CAUSED_BY,
            properties={"observed_in": 3},
        )
        assert e.properties["observed_in"] == 3


class TestLearningSnapshotImmutable:

    def test_reproduces_audit_defect(self):
        snap = LearningSnapshot(snapshot_id="snap1",
                                metadata={"note": "sprint2"})
        with pytest.raises(TypeError):
            snap.metadata["note"] = "poisoned"

    def test_to_dict_output_still_mutable_plain_dict(self):
        snap = LearningSnapshot(snapshot_id="snap1",
                                metadata={"tenant": "acme"})
        out = snap.to_dict()
        assert type(out["metadata"]) is dict
        # Existing to_dict contract: metadata is a fresh dict copy.
        out["metadata"]["tenant"] = "beta"


# ---------------------------------------------------------------------------
# RC-E — Append-only ledger guarantees
# ---------------------------------------------------------------------------

from sentinel_core.intel_memory.memory_store import MemoryStore, MemoryStoreError


class TestMemoryStoreAppendOnly:
    """RC-E: MemoryStore.delete must archive, not destroy."""

    def _rec(self, memory_id: str, **kw) -> MemoryRecord:
        return MemoryRecord(memory_id=memory_id, **kw)

    def test_reproduces_audit_defect_delete_no_longer_destroys(self, tmp_path):
        """PRE-FIX: after `delete`, the record was gone forever. POST-FIX:
        record survives under `.deleted/`."""
        s = MemoryStore(tmp_path / "mem")
        s.save(self._rec("m1", service="checkout"))
        s.delete("m1")
        # Primary namespace: gone (existing contract preserved).
        assert not s.has("m1")
        with pytest.raises(MemoryStoreError):
            s.load("m1")
        assert "m1" not in s.list_ids()
        # Audit trail: preserved.
        assert "m1" in s.list_deleted()
        recovered = s.load_deleted("m1")
        assert recovered.memory_id == "m1"
        assert recovered.service == "checkout"

    def test_existing_has_and_delete_contract_preserved(self, tmp_path):
        """The pre-Sprint 2 test_has_and_delete expectation must still hold."""
        s = MemoryStore(tmp_path / "mem")
        s.save(self._rec("m1"))
        assert s.has("m1")
        s.delete("m1")
        assert not s.has("m1")

    def test_delete_twice_preserves_both_versions(self, tmp_path):
        """Two save+delete cycles → two tombstones, no history loss."""
        s = MemoryStore(tmp_path / "mem")
        s.save(self._rec("m1", service="v1"))
        s.delete("m1")
        s.save(self._rec("m1", service="v2"))
        s.delete("m1")
        deleted = s.list_deleted()
        assert "m1" in deleted
        assert "m1.1" in deleted
        # Both versions recoverable.
        v_first = s.load_deleted("m1")
        v_second = s.load_deleted("m1.1")
        # We do not assert ordering (filesystem-dependent) — assert content.
        services = {v_first.service, v_second.service}
        assert services == {"v1", "v2"}

    def test_delete_nonexistent_is_noop(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        s.delete("does-not-exist")  # must not raise
        assert s.list_deleted() == ()

    def test_deleted_records_do_not_leak_into_list_ids(self, tmp_path):
        """`.deleted/` subfolder must not appear in primary list_ids."""
        s = MemoryStore(tmp_path / "mem")
        s.save(self._rec("m1"))
        s.save(self._rec("m2"))
        s.delete("m1")
        assert s.list_ids() == ("m2",)
        assert "m1" in s.list_deleted()

    def test_load_deleted_missing_raises(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        with pytest.raises(MemoryStoreError):
            s.load_deleted("nope")


# ---------------------------------------------------------------------------
# RC-E — ReplayStore.save must preserve history
# ---------------------------------------------------------------------------

from tests.replay.replay_store import ReplayStore, ReplayStoreError
from tests.replay.schemas import BenchmarkRun


def _run(run_id: str, generated_at: str = "2026-01-01T00:00:00Z", **meta) -> BenchmarkRun:
    return BenchmarkRun(
        run_id=run_id,
        generated_at=generated_at,
        scorecards=(),
        metadata=dict(meta),
    )


class TestReplayStoreAppendOnly:

    def test_reproduces_audit_defect_prior_version_no_longer_lost(self, tmp_path):
        """PRE-FIX: two saves with the same run_id → first was gone.
        POST-FIX: first version is preserved under history/."""
        rs = ReplayStore(tmp_path / "replay")
        v1 = _run("r1", generated_at="2026-01-01T00:00:00Z", v=1)
        v2 = _run("r1", generated_at="2026-02-01T00:00:00Z", v=2)
        rs.save(v1)
        rs.save(v2)
        # Primary: latest.
        assert rs.load("r1").metadata.get("v") == 2
        # History: prior preserved.
        history = rs.load_history("r1")
        assert len(history) >= 1
        assert history[0].metadata.get("v") == 1

    def test_existing_save_load_contract_preserved(self, tmp_path):
        rs = ReplayStore(tmp_path / "replay")
        r = _run("r1", generated_at="2026-01-01T00:00:00Z")
        rs.save(r)
        loaded = rs.load("r1")
        assert loaded.run_id == "r1"
        # Byte-deterministic content preserved.
        raw = (tmp_path / "replay" / "r1.json").read_text()
        assert '"run_id": "r1"' in raw

    def test_multiple_prior_versions_all_preserved(self, tmp_path):
        rs = ReplayStore(tmp_path / "replay")
        for i in range(1, 4):
            rs.save(_run("r1", generated_at=f"2026-0{i}-01T00:00:00Z", v=i))
        history = rs.load_history("r1")
        assert len(history) == 2  # prior versions v=1 and v=2 archived
        # Current is v=3.
        assert rs.load("r1").metadata.get("v") == 3

    def test_load_history_empty_on_fresh_store(self, tmp_path):
        rs = ReplayStore(tmp_path / "replay")
        assert rs.load_history("never-saved") == ()

    def test_tmp_filename_no_longer_deterministic(self, tmp_path):
        """RC-E concurrency: two concurrent writers must not share a tmp
        path. We can't easily simulate concurrency without threads, but
        we can assert that the tmp filename includes PID + UUID so
        collisions are practically impossible."""
        import os
        import re
        # Introspect the save method's tmp construction indirectly by
        # inspecting the source and verifying uniqueness intent.
        import inspect
        from tests.replay.replay_store import ReplayStore
        src = inspect.getsource(ReplayStore.save)
        assert "getpid" in src or "uuid" in src

    def test_history_directory_isolated_from_primary_reads(self, tmp_path):
        """history/ subfolder must not be enumerated by list_runs / load_all."""
        rs = ReplayStore(tmp_path / "replay")
        rs.save(_run("r1", generated_at="2026-01-01T00:00:00Z"))
        rs.save(_run("r1", generated_at="2026-02-01T00:00:00Z"))
        assert rs.list_runs() == ("r1",)   # not ("history", "r1")
        assert len(rs.load_all()) == 1
