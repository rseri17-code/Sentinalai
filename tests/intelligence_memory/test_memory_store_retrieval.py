"""MemoryStore + Retrieval tests."""
from __future__ import annotations

import pytest

from sentinel_core.intel_memory import (
    BlastRadiusSnapshot,
    MemoryRecord,
    MemoryStore,
    MemoryStoreError,
    Retrieval,
    TopologySnapshot,
)


def _rec(mid: str, **k) -> MemoryRecord:
    defaults = dict(memory_id=mid)
    defaults.update(k)
    return MemoryRecord(**defaults)


class TestMemoryStore:
    def test_empty_lists_nothing(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        assert s.list_ids() == ()
        assert s.load_all() == ()

    def test_save_and_load(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        s.save(_rec("m1", service="checkout", confidence=80))
        r = s.load("m1")
        assert r.memory_id == "m1"
        assert r.service == "checkout"
        assert r.confidence == 80

    def test_list_ids_sorted(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        s.save(_rec("mb"))
        s.save(_rec("ma"))
        assert s.list_ids() == ("ma", "mb")

    def test_has_and_delete(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        s.save(_rec("m1"))
        assert s.has("m1")
        s.delete("m1")
        assert not s.has("m1")

    def test_deterministic_write(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        r = _rec("m1", service="checkout")
        path = s.save(r)
        bytes1 = path.read_bytes()
        s.save(r)   # rewrite
        assert path.read_bytes() == bytes1

    def test_invalid_ids_rejected(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        with pytest.raises(MemoryStoreError):
            s.save(_rec(""))
        with pytest.raises(MemoryStoreError):
            s.load("bad/id")

    def test_missing_load_raises(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        with pytest.raises(MemoryStoreError):
            s.load("nope")


class TestRetrieval:
    def _seed(self, tmp_path):
        s = MemoryStore(tmp_path / "mem")
        s.save(_rec("m1", service="checkout", application="ecom",
                     incident_type="saturation", confidence=80,
                     mtti_ms=45000, fingerprint="fp-1",
                     timestamp="2026-07-01T00:00:00Z",
                     topology=TopologySnapshot(services=("checkout", "db"),
                                                 namespaces=("prod",)),
                     transaction_path=("ui", "checkout"),
                     planner_decisions=("cap:collect_logs",),
                     detected_root_cause="database pool exhausted",
                     skills_used=("kubectl_pods", "git_history:abc"),))
        s.save(_rec("m2", service="checkout", application="ecom",
                     incident_type="network", confidence=60,
                     mtti_ms=90000, fingerprint="fp-2",
                     timestamp="2026-07-02T00:00:00Z",
                     topology=TopologySnapshot(services=("checkout",)),
                     transaction_path=("checkout", "db"),
                     planner_decisions=("cap:collect_dns_state",),
                     detected_root_cause="dns nxdomain"))
        s.save(_rec("m3", service="payments", application="fintech",
                     incident_type="authentication", confidence=40,
                     mtti_ms=30000, fingerprint="fp-3",
                     timestamp="2026-07-03T00:00:00Z"))
        return s

    def test_by_fingerprint(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_fingerprint("fp-1")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_service(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_service("checkout")
        assert tuple(r.memory_id for r in got) == ("m1", "m2")

    def test_by_application(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_application("fintech")
        assert tuple(r.memory_id for r in got) == ("m3",)

    def test_by_incident_type(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_incident_type("saturation")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_topology_service(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_topology_service("db")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_deployment(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_deployment("git_history")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_namespace(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_namespace("prod")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_root_cause_contains(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_root_cause_contains("pool")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_transaction_path_hop(self, tmp_path):
        s = self._seed(tmp_path)
        # Only m2 has "db" in its transaction path (m1 has ("ui", "checkout"))
        got = Retrieval(s).by_transaction_path("db")
        assert tuple(r.memory_id for r in got) == ("m2",)

    def test_by_planner_capability(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_planner_capability("cap:collect_logs")
        assert tuple(r.memory_id for r in got) == ("m1",)

    def test_by_confidence_range(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_confidence_range(50, 100)
        assert tuple(r.memory_id for r in got) == ("m1", "m2")

    def test_by_mtti_range(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_mtti_range(40000, 100000)
        assert tuple(r.memory_id for r in got) == ("m1", "m2")

    def test_by_time_window(self, tmp_path):
        s = self._seed(tmp_path)
        got = Retrieval(s).by_time_window("2026-07-02", "2026-07-03T23:59:59Z")
        assert tuple(r.memory_id for r in got) == ("m2", "m3")
