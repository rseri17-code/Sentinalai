"""Tests for CoFailureIndex (Phase 2 harness learning loop)."""
from __future__ import annotations

import pytest


class TestCoFailureStats:
    def test_co_failure_rate_zero_when_no_incidents(self):
        from supervisor.co_failure_index import CoFailureStats
        s = CoFailureStats(service_a="a", service_b="b")
        assert s.co_failure_rate == 0.0

    def test_co_failure_rate_computed(self):
        from supervisor.co_failure_index import CoFailureStats
        s = CoFailureStats(service_a="a", service_b="b",
                           co_failure_count=3, total_incidents_a=10)
        assert s.co_failure_rate == pytest.approx(0.3)

    def test_roundtrip(self):
        from supervisor.co_failure_index import CoFailureStats
        s = CoFailureStats(service_a="a", service_b="b",
                           co_failure_count=5, total_incidents_a=10,
                           avg_delay_seconds=30.0)
        assert CoFailureStats.from_dict(s.to_dict()).avg_delay_seconds == pytest.approx(30.0)


class TestCoFailureIndex:
    def test_record_and_retrieve(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        idx = CoFailureIndex(str(tmp_path / "idx.json"))
        idx.record_investigation("payment-api", ["db", "cache"])
        results = idx.get_co_failures("payment-api", min_rate=0.0)
        assert len(results) == 2
        names = {(r.service_a, r.service_b) for r in results}
        assert any("payment-api" in pair for pair in names)

    def test_co_failure_rate_after_multiple_incidents(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        idx = CoFailureIndex(str(tmp_path / "idx.json"))
        # 3 incidents where payment-api + db co-fail, 2 where payment-api alone fails
        for _ in range(3):
            idx.record_investigation("payment-api", ["db"])
        for _ in range(2):
            idx.record_investigation("payment-api", [])  # no co-failure
        results = idx.get_co_failures("payment-api", min_rate=0.0)
        assert len(results) == 1
        assert results[0].co_failure_rate == pytest.approx(3 / 5)

    def test_min_rate_filter(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        idx = CoFailureIndex(str(tmp_path / "idx.json"))
        idx.record_investigation("svc-a", ["svc-b"])
        # Only 1 incident, co_failure_rate = 1.0; filter at 0.5 should pass
        assert len(idx.get_co_failures("svc-a", min_rate=0.5)) == 1
        # Filter at 1.1 should block
        assert len(idx.get_co_failures("svc-a", min_rate=1.1)) == 0

    def test_self_not_recorded(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        idx = CoFailureIndex(str(tmp_path / "idx.json"))
        idx.record_investigation("svc-a", ["svc-a", "svc-b"])
        results = idx.get_co_failures("svc-a", min_rate=0.0)
        # svc-a should not appear as a co-failure of itself
        for r in results:
            assert r.service_a != r.service_b

    def test_delay_averaging(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        idx = CoFailureIndex(str(tmp_path / "idx.json"))
        idx.record_investigation("a", ["b"], delay_seconds=10.0)
        idx.record_investigation("a", ["b"], delay_seconds=20.0)
        results = idx.get_co_failures("a", min_rate=0.0)
        assert results[0].avg_delay_seconds == pytest.approx(15.0)

    def test_persistence_roundtrip(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        path = str(tmp_path / "idx.json")
        idx1 = CoFailureIndex(path)
        idx1.record_investigation("x", ["y"])

        idx2 = CoFailureIndex(path)
        assert len(idx2.get_co_failures("x", min_rate=0.0)) == 1

    def test_empty_service_ignored(self, tmp_path):
        from supervisor.co_failure_index import CoFailureIndex
        idx = CoFailureIndex(str(tmp_path / "idx.json"))
        idx.record_investigation("", ["b"])   # empty primary → noop
        idx.record_investigation("a", [""])   # empty co-failure → ignored
        assert idx.get_co_failures("a", min_rate=0.0) == []
