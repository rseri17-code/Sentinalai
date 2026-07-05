"""LearningLoop + GuidedInvestigation + report renderers tests."""
from __future__ import annotations

import json

import pytest

from sentinel_core.intel_memory import (
    BlastRadiusSnapshot,
    GuidedInvestigation,
    LearningLoop,
    MemoryRecord,
    Ranker,
    RecurringPatternKind,
    SimilarityEngine,
    TopologySnapshot,
)
from sentinel_core.intel_memory.report import (
    render_experience_reuse,
    render_guided_investigation,
    render_incident_clusters,
    render_knowledge_growth,
    render_learning_report,
    render_master_report,
    render_memory_report,
    render_mtti_improvement,
    render_similarity_report,
    render_top_false_leads,
    render_top_root_causes,
    to_json,
)


def _rec(mid, **k):
    d = dict(memory_id=mid)
    d.update(k)
    return MemoryRecord(**d)


# ---------------------------------------------------------------------------
# LearningLoop
# ---------------------------------------------------------------------------

class TestLearningLoop:
    def test_empty_corpus(self):
        assert LearningLoop().all_patterns(()) == ()

    def test_recurring_root_causes(self):
        r1 = _rec("m1", detected_root_cause="database pool exhausted")
        r2 = _rec("m2", detected_root_cause="database pool exhausted")
        r3 = _rec("m3", detected_root_cause="dns nxdomain")
        p = LearningLoop(min_count=2).recurring_root_causes((r1, r2, r3))
        assert len(p) == 1
        assert p[0].count == 2
        assert p[0].kind == RecurringPatternKind.ROOT_CAUSE.value

    def test_recurring_evidence(self):
        r1 = _rec("m1", evidence_collected=("logs", "metrics"))
        r2 = _rec("m2", evidence_collected=("logs", "metrics"))
        p = LearningLoop(min_count=2).recurring_evidence((r1, r2))
        assert len(p) == 1
        assert p[0].count == 2

    def test_min_count_respected(self):
        r1 = _rec("m1", detected_root_cause="foo")
        r2 = _rec("m2", detected_root_cause="bar")
        assert LearningLoop(min_count=2).recurring_root_causes((r1, r2)) == ()

    def test_recurring_false_leads(self):
        r1 = _rec("m1", false_leads=("certificate",))
        r2 = _rec("m2", false_leads=("certificate",))
        r3 = _rec("m3", false_leads=("dns", "certificate"))
        p = LearningLoop(min_count=2).recurring_false_leads((r1, r2, r3))
        assert len(p) == 1
        assert p[0].signature == "certificate"
        assert p[0].count == 3

    def test_recurring_missing_evidence(self):
        canon = ("logs", "metrics", "traces")
        r1 = _rec("m1", evidence_collected=("logs",))
        r2 = _rec("m2", evidence_collected=("metrics",))
        p = LearningLoop(min_count=2).recurring_missing_evidence((r1, r2), canon)
        sigs = {x.signature for x in p}
        # both records miss "traces" → recurring
        assert "traces" in sigs

    def test_recurring_mtti_bottlenecks(self):
        r1 = _rec("m1", mtti_ms=200000, incident_type="saturation")
        r2 = _rec("m2", mtti_ms=300000, incident_type="saturation")
        p = LearningLoop(min_count=2).recurring_mtti_bottlenecks((r1, r2), mtti_threshold_ms=120000)
        assert len(p) == 1

    def test_all_patterns_deterministic(self):
        r1 = _rec("m1", detected_root_cause="foo", evidence_collected=("logs",))
        r2 = _rec("m2", detected_root_cause="foo", evidence_collected=("logs",))
        loop = LearningLoop()
        p1 = loop.all_patterns((r1, r2))
        p2 = loop.all_patterns((r1, r2))
        assert [x.to_dict() for x in p1] == [x.to_dict() for x in p2]


# ---------------------------------------------------------------------------
# GuidedInvestigation
# ---------------------------------------------------------------------------

class TestGuidedInvestigation:
    def _corpus(self):
        return (
            _rec("m1", service="checkout", incident_type="saturation",
                  evidence_collected=("logs", "metrics"),
                  evidence_ordering=("logs", "metrics"),
                  planner_decisions=("cap:collect_logs", "cap:collect_metrics"),
                  detected_root_cause="pool exhausted",
                  verified_root_cause="pool exhausted",
                  resolution="scale pool",
                  confidence=85, mtti_ms=45000,
                  investigation_score=0.9,
                  blast_radius=BlastRadiusSnapshot(severity="high", total_affected=3)),
            _rec("m2", service="checkout", incident_type="saturation",
                  evidence_collected=("logs", "metrics"),
                  evidence_ordering=("logs", "metrics"),
                  planner_decisions=("cap:collect_logs", "cap:collect_metrics"),
                  detected_root_cause="pool exhausted",
                  resolution="restart",
                  confidence=80, mtti_ms=50000,
                  investigation_score=0.85,
                  blast_radius=BlastRadiusSnapshot(severity="high", total_affected=2)),
        )

    def test_have_seen_before(self):
        query = _rec("q", service="checkout", incident_type="saturation",
                       evidence_collected=("logs",))
        g = GuidedInvestigation().build(query, self._corpus())
        assert g["have_seen_this_before"] is True
        assert g["top_similar"]

    def test_evidence_overlap(self):
        query = _rec("q", service="checkout",
                       evidence_collected=("logs", "metrics"))
        g = GuidedInvestigation().build(query, self._corpus())
        assert g["evidence_overlap"]["average_overlap"] == 2.0

    def test_recommended_order(self):
        query = _rec("q", service="checkout")
        g = GuidedInvestigation().build(query, self._corpus())
        # Most-common ordering wins
        assert g["recommended_investigation_order"] == ["logs", "metrics"]

    def test_expected_confidence_mtti(self):
        query = _rec("q", service="checkout")
        g = GuidedInvestigation().build(query, self._corpus())
        assert g["expected_confidence"] > 0
        assert g["expected_mtti_ms"] > 0

    def test_empty_candidates_have_seen_false(self):
        query = _rec("q", service="checkout")
        g = GuidedInvestigation().build(query, ())
        assert g["have_seen_this_before"] is False
        assert g["top_similar"] == []

    def test_known_root_causes_and_resolutions(self):
        query = _rec("q", service="checkout")
        g = GuidedInvestigation().build(query, self._corpus())
        assert "pool exhausted" in g["known_root_causes"]
        assert "scale pool" in g["known_resolutions"]


# ---------------------------------------------------------------------------
# Report renderers + master report
# ---------------------------------------------------------------------------

class TestReports:
    def _records(self):
        return (
            _rec("m1", service="checkout", incident_type="saturation",
                  fingerprint="fp-x", timestamp="2026-07-01T00:00:00Z",
                  mtti_ms=60000, confidence=80,
                  detected_root_cause="pool exhausted"),
            _rec("m2", service="checkout", incident_type="saturation",
                  fingerprint="fp-x",  # same fingerprint → reused
                  timestamp="2026-07-02T00:00:00Z",
                  mtti_ms=30000, confidence=90,
                  detected_root_cause="pool exhausted",
                  false_leads=("certificate",)),
            _rec("m3", service="payments", incident_type="network",
                  fingerprint="fp-y", timestamp="2026-07-03T00:00:00Z",
                  mtti_ms=45000, confidence=60,
                  detected_root_cause="dns nxdomain",
                  false_leads=("certificate",)),
        )

    def test_memory_report(self):
        r = render_memory_report(self._records())
        assert r["record_count"] == 3

    def test_similarity_report(self):
        recs = self._records()
        query = recs[0]
        r = render_similarity_report(query, recs, top_n=5)
        assert r["query_id"] == "m1"
        assert len(r["matches"]) <= 5

    def test_learning_report(self):
        r = render_learning_report(self._records())
        assert r["record_count"] == 3
        # "pool exhausted" appears twice → root_cause pattern present
        kinds = {p["kind"] for p in r["patterns"]}
        assert "root_cause" in kinds

    def test_incident_clusters(self):
        r = render_incident_clusters(self._records())
        assert r["cluster_count"] == 2

    def test_knowledge_growth_monotonic(self):
        r = render_knowledge_growth(self._records())
        counts = [row["unique_fingerprint_count"] for row in r["growth"]]
        assert counts == sorted(counts)

    def test_experience_reuse_rate(self):
        r = render_experience_reuse(self._records())
        # 2 records share fp-x
        assert r["reused_records"] == 2
        assert r["reuse_rate"] == round(2 / 3, 4)

    def test_top_root_causes(self):
        r = render_top_root_causes(self._records())
        first = r["top_root_causes"][0]
        assert first["root_cause"] == "pool exhausted"
        assert first["count"] == 2

    def test_top_false_leads(self):
        r = render_top_false_leads(self._records())
        assert r["top_false_leads"][0]["lead"] == "certificate"
        assert r["top_false_leads"][0]["count"] == 2

    def test_mtti_improvement(self):
        r = render_mtti_improvement(self._records())
        assert r["average_mtti_ms"] > 0

    def test_guided_investigation_report(self):
        recs = self._records()
        r = render_guided_investigation(recs[0], recs, top_n=5)
        assert "top_similar" in r

    def test_master_report_bundles_all(self):
        recs = self._records()
        r = render_master_report(recs, query=recs[0])
        for key in ("memory_report", "learning_report", "recurring_patterns",
                     "incident_clusters", "knowledge_growth",
                     "experience_reuse", "top_root_causes",
                     "top_false_leads", "mtti_improvement",
                     "similarity_report", "guided_investigation"):
            assert key in r
        # deterministic JSON round-trip
        json.loads(to_json(r))

    def test_master_report_deterministic(self):
        recs = self._records()
        j1 = to_json(render_master_report(recs))
        j2 = to_json(render_master_report(recs))
        assert j1 == j2
