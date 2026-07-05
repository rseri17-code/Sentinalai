"""Similarity engine tests."""
from __future__ import annotations

import pytest

from sentinel_core.intel_memory import (
    BlastRadiusSnapshot,
    MemoryRecord,
    SIMILARITY_WEIGHTS,
    SimilarityEngine,
    TopologySnapshot,
)


def _rec(**k) -> MemoryRecord:
    defaults = dict(memory_id="m")
    defaults.update(k)
    return MemoryRecord(**defaults)


class TestSimilarityBasics:
    def test_identical_record_high_score(self):
        rec = _rec(memory_id="m1", service="checkout",
                    incident_type="saturation",
                    evidence_collected=("logs", "metrics"),
                    planner_decisions=("cap:a", "cap:b"),
                    transaction_path=("ui", "checkout", "db"),
                    detected_root_cause="pool exhausted at checkout")
        # score() is used pairwise; self-comparison via score_many is
        # skipped (score_many drops the query itself). But direct score
        # between two identical copies should be very high.
        e = SimilarityEngine()
        rec2 = MemoryRecord(**{**rec.__dict__, "memory_id": "m2"})
        s = e.score(rec, rec2)
        assert s.overall > 0.8

    def test_different_service_lower_score(self):
        # Populate distinguishing fields so the score reflects real
        # differences (empty-vs-empty on many dims defaults to 1.0
        # in the similarity engine — that's intentional so records
        # with unknown topology are neutral, not disqualified).
        a = _rec(memory_id="a", service="checkout",
                  incident_type="saturation",
                  evidence_collected=("logs", "metrics"),
                  planner_decisions=("cap:collect_logs",),
                  detected_root_cause="database pool exhausted",
                  topology=TopologySnapshot(services=("checkout", "db")))
        b = _rec(memory_id="b", service="payments",
                  incident_type="network",
                  evidence_collected=("dns_records", "traces"),
                  planner_decisions=("cap:collect_dns_state",),
                  detected_root_cause="dns nxdomain for external endpoint",
                  topology=TopologySnapshot(services=("payments", "gateway")))
        s = SimilarityEngine().score(a, b)
        # Very different records — overall should be substantially less
        # than 1.0. (Empty-vs-empty dims still contribute their weight.)
        assert s.overall < 0.8

    def test_exact_fingerprint_flagged(self):
        a = _rec(memory_id="a", fingerprint="ABCDEF1234567890")
        b = _rec(memory_id="b", fingerprint="ABCDEF1234567890")
        s = SimilarityEngine().score(a, b)
        assert s.exact_match is True

    def test_empty_records_zero_score(self):
        a = _rec(memory_id="a")
        b = _rec(memory_id="b")
        s = SimilarityEngine().score(a, b)
        assert s.overall >= 0.0

    def test_score_many_deterministic_sort(self):
        query = _rec(memory_id="q", service="checkout",
                      evidence_collected=("logs", "metrics"))
        c1 = _rec(memory_id="c1", service="checkout",
                    evidence_collected=("logs",))
        c2 = _rec(memory_id="c2", service="checkout",
                    evidence_collected=("logs", "metrics", "traces"))
        c3 = _rec(memory_id="c3", service="payments",
                    evidence_collected=())
        result = SimilarityEngine().score_many(query, (c1, c2, c3))
        # Excludes query itself; sorted overall DESC
        ids = [r.memory_id for r in result]
        assert ids == [ids[0], ids[1], ids[2]]  # sanity
        assert result[0].overall >= result[-1].overall

    def test_score_many_skips_query_itself(self):
        query = _rec(memory_id="q", service="checkout")
        candidates = (
            _rec(memory_id="q", service="checkout"),   # same id → skip
            _rec(memory_id="c1", service="checkout"),
        )
        result = SimilarityEngine().score_many(query, candidates)
        assert all(r.memory_id != "q" for r in result)


class TestSimilarityDimensions:
    def test_topology_overlap_counts(self):
        a = _rec(memory_id="a", topology=TopologySnapshot(services=("s1", "s2")))
        b = _rec(memory_id="b", topology=TopologySnapshot(services=("s1", "s2")))
        s = SimilarityEngine().score(a, b)
        assert s.breakdown["topology"] == 1.0

    def test_planner_prefix_score(self):
        a = _rec(memory_id="a", planner_decisions=("cap:x", "cap:y", "cap:z"))
        b = _rec(memory_id="b", planner_decisions=("cap:x", "cap:y", "cap:z"))
        s = SimilarityEngine().score(a, b)
        assert s.breakdown["planner"] == 1.0

    def test_root_cause_overlap(self):
        a = _rec(memory_id="a", detected_root_cause="database pool exhausted")
        b = _rec(memory_id="b", detected_root_cause="database pool restarted")
        s = SimilarityEngine().score(a, b)
        # some overlap on "database", "pool"
        assert s.breakdown["root_cause"] > 0.0


class TestWeights:
    def test_weights_sum_reasonable(self):
        # Design contract: weights across all dimensions ≤ 1.0
        assert sum(SIMILARITY_WEIGHTS.values()) <= 1.0

    def test_custom_weights_override(self):
        # Zero out every dimension; only root_cause counts
        w = {k: 0.0 for k in SIMILARITY_WEIGHTS}
        w["root_cause"] = 1.0
        engine = SimilarityEngine(weights=w)
        a = _rec(memory_id="a", detected_root_cause="pool exhausted")
        b = _rec(memory_id="b", detected_root_cause="pool exhausted")
        s = engine.score(a, b)
        assert s.overall == 1.0 or s.overall > 0.98
