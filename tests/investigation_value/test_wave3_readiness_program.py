"""Wave 3 Readiness Program — regression suite.

WS1 artifact enrichment (B2), WS2 projection honesty (B3),
WS3 similarity NOT-MEASURED (B1), WS4 value metrics, WS5 gates,
WS6 shadow pipeline.
"""
from __future__ import annotations

import json

import pytest

from sentinel_core.intel_memory import (
    MemoryRecord,
    MemoryStore,
    SimilarityEngine,
    TopologySnapshot,
)
from sentinel_core.investigation_artifact import (
    ArtifactStore,
    artifact_from_dict,
    artifact_to_dict,
    build_artifact,
)
from sentinel_core.investigation_value import (
    GateInputs,
    confidence_gain,
    evaluate_gates,
    evidence_acceleration_score,
    false_lead_elimination_score,
    investigation_improvement_potential,
    planner_guidance_score,
    root_cause_acceleration_score,
    run_readiness_evaluation,
    worker_reduction_score,
)

from tests.investigation_artifact.test_wave1_artifact import _full_result


def _enriched_result(**overrides):
    """Result carrying the B2 enrichment sources (rca_report, remediation,
    critique gaps)."""
    r = _full_result(**overrides)
    r["rca_report"] = {
        "affected_service": "checkout",
        "incident_type": "saturation",
        "severity_label": "high",
        "tool_calls_made": 17,
    }
    r["remediation"] = "increase pool size to 200 and fix connection leak"
    r["_critique"] = {"score": 0.8, "dimensions": {},
                       "gaps": ["deployment", "certificate"]}
    return r


def _build(result=None, **kw):
    defaults = dict(incident_id="INC-1", investigation_id="inv-abc",
                     created_at="2026-07-07T00:00:02Z",
                     provenance={"producer": "wave1"})
    defaults.update(kw)
    return build_artifact(result or _enriched_result(), **defaults)


# ---------------------------------------------------------------------------
# WS1 — Artifact enrichment (B2)
# ---------------------------------------------------------------------------

class TestArtifactEnrichment:
    def test_identity_and_outcome_captured(self):
        a = _build()
        assert a.service == "checkout"
        assert a.incident_type == "saturation"
        assert a.severity == "high"
        assert a.runtime_cost == 17
        assert "pool size" in a.resolution
        assert a.false_leads == ("deployment", "certificate")

    def test_decision_summary_fallback_when_no_rca_report(self):
        r = _full_result()
        r["_phase_receipts"][3]["metadata"]["intelligence"] = [
            {"name": "decision_intelligence",
             "payload": {"service": "payments", "incident_type": "network"}},
        ]
        a = _build(r)
        assert a.service == "payments"
        assert a.incident_type == "network"

    def test_graceful_when_sources_absent(self):
        a = _build(_full_result())
        assert a.service == ""
        assert a.resolution == ""
        assert a.false_leads == ()
        assert a.runtime_cost == 0

    def test_deterministic_id_with_enrichment(self):
        assert _build().artifact_id == _build().artifact_id

    def test_id_changes_when_enrichment_changes(self):
        r2 = _enriched_result()
        r2["rca_report"]["affected_service"] = "payments"
        assert _build().artifact_id != _build(r2).artifact_id

    def test_pre_enrichment_artifact_still_readable(self):
        """Additive schema evolution: dicts without the new keys load."""
        d = artifact_to_dict(_build())
        for k in ("service", "incident_type", "severity", "environment",
                   "application", "resolution", "false_leads",
                   "runtime_cost"):
            d.pop(k, None)
        old = artifact_from_dict(d)
        assert old.service == ""
        assert old.false_leads == ()
        assert old.schema_version == 1
        assert old.root_cause == _build().root_cause

    def test_round_trip_with_enrichment(self):
        a = _build()
        d = artifact_to_dict(a)
        assert d == json.loads(json.dumps(d))
        a2 = artifact_from_dict(d)
        assert json.dumps(artifact_to_dict(a2), sort_keys=True) \
            == json.dumps(d, sort_keys=True)

    def test_dict_gap_serialised_canonically(self):
        r = _enriched_result()
        r["_critique"]["gaps"] = [{"kind": "missing", "key": "iops"}]
        a = _build(r)
        assert a.false_leads == ('{"key":"iops","kind":"missing"}',)


# ---------------------------------------------------------------------------
# WS2 — Projection honesty (B3)
# ---------------------------------------------------------------------------

class TestProjectionHonesty:
    def test_evidence_ordering_never_fabricated(self):
        r = MemoryRecord.from_artifact(_build())
        assert r.evidence_ordering == ()          # truthful empty
        assert r.evidence_collected != ()          # keys still projected

    def test_enrichment_reaches_memory_record(self):
        r = MemoryRecord.from_artifact(_build())
        assert r.service == "checkout"
        assert r.incident_type == "saturation"
        assert r.severity == "high"
        assert r.resolution.startswith("increase pool size")
        assert r.false_leads == ("deployment", "certificate")
        assert r.runtime_cost == 17

    def test_projection_still_deterministic(self):
        a = _build()
        r1, r2 = MemoryRecord.from_artifact(a), MemoryRecord.from_artifact(a)
        assert json.dumps(r1.to_dict(), sort_keys=True) \
            == json.dumps(r2.to_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# WS3 — Similarity NOT-MEASURED (B1)
# ---------------------------------------------------------------------------

def _rec(**kw) -> MemoryRecord:
    base = dict(memory_id="m", fingerprint="")
    base.update(kw)
    return MemoryRecord(**base)


class TestSimilarityNotMeasured:
    def test_empty_vs_empty_is_not_a_match(self):
        """The audit's centerpiece defect: two sparse records must not
        score the vacuous 0.41 floor."""
        a = _rec(memory_id="a", service="checkout",
                  detected_root_cause="pool exhausted",
                  evidence_collected=("logs",))
        b = _rec(memory_id="b", service="payments",
                  detected_root_cause="dns nxdomain",
                  evidence_collected=("traces",))
        s = SimilarityEngine().score(a, b)
        # topology/dependency/transaction/resolution/blast/exact all
        # NOT MEASURED; measured dims (root_cause, evidence) are 0.
        assert s.overall == 0.0
        assert "topology" in s.not_measured
        assert "blast_radius" in s.not_measured
        assert "exact" in s.not_measured

    def test_fully_empty_records_score_zero_everything_unmeasured(self):
        s = SimilarityEngine().score(_rec(memory_id="a"),
                                       _rec(memory_id="b"))
        assert s.overall == 0.0
        assert len(s.not_measured) == 10           # all dimensions
        assert s.breakdown == {}

    def test_measured_dimensions_renormalized(self):
        """Only root_cause (0.15) and evidence (0.15) measured, both
        1.0 → renormalized overall = 1.0, not 0.30."""
        a = _rec(memory_id="a", detected_root_cause="pool exhausted",
                  evidence_collected=("logs", "iops"))
        b = _rec(memory_id="b", detected_root_cause="pool exhausted",
                  evidence_collected=("logs", "iops"))
        s = SimilarityEngine().score(a, b)
        assert s.overall == 1.0
        assert set(s.breakdown.keys()) == {"root_cause", "evidence"}

    def test_one_sided_empty_is_measured_zero(self):
        a = _rec(memory_id="a", evidence_collected=("logs",))
        b = _rec(memory_id="b")
        s = SimilarityEngine().score(a, b)
        assert s.breakdown.get("evidence") == 0.0   # measured, not skipped
        assert "evidence" not in s.not_measured

    def test_exact_unmeasured_without_fingerprints(self):
        s = SimilarityEngine().score(_rec(memory_id="a"),
                                       _rec(memory_id="b"))
        assert "exact" in s.not_measured
        assert s.exact_match is False

    def test_default_blast_radius_not_a_vacuous_match(self):
        a = _rec(memory_id="a", evidence_collected=("logs",))
        b = _rec(memory_id="b", evidence_collected=("logs",))
        s = SimilarityEngine().score(a, b)
        assert "blast_radius" in s.not_measured

    def test_explainability_in_to_dict(self):
        s = SimilarityEngine().score(_rec(memory_id="a"),
                                       _rec(memory_id="b"))
        d = s.to_dict()
        assert "not_measured" in d
        assert isinstance(d["not_measured"], list)

    def test_deterministic_and_byte_identical_ordering(self):
        recs = tuple(
            _rec(memory_id=f"m{i}", service="s",
                  detected_root_cause=f"cause {i} pool",
                  evidence_collected=("logs", f"k{i}"))
            for i in range(5)
        )
        q = _rec(memory_id="q", service="s",
                  detected_root_cause="cause pool exhausted",
                  evidence_collected=("logs",))
        e = SimilarityEngine()
        r1 = e.score_many(q, recs)
        r2 = e.score_many(q, recs)
        assert json.dumps([s.to_dict() for s in r1], sort_keys=True) \
            == json.dumps([s.to_dict() for s in r2], sort_keys=True)
        # highest first, memory_id tiebreak
        overalls = [s.overall for s in r1]
        assert overalls == sorted(overalls, reverse=True)

    def test_topology_populated_still_measured(self):
        a = _rec(memory_id="a",
                  topology=TopologySnapshot(services=("checkout", "db")))
        b = _rec(memory_id="b",
                  topology=TopologySnapshot(services=("checkout", "web")))
        s = SimilarityEngine().score(a, b)
        assert "topology" in s.breakdown
        assert s.breakdown["topology"] == pytest.approx(1 / 3, abs=0.001)


# ---------------------------------------------------------------------------
# WS4 — Investigation value metrics
# ---------------------------------------------------------------------------

class TestValueMetrics:
    def test_iip_perfect_retrieval(self):
        v = investigation_improvement_potential(
            retrieved_root_causes=("database pool exhausted",),
            confirmed_root_cause="database pool exhausted",
            retrieved_evidence=("logs", "iops"),
            decisive_evidence=("logs", "iops"),
            retrieved_false_leads=("deployment",),
            ruled_out_hypotheses=("deployment",),
        )
        assert v == 1.0

    def test_iip_zero_when_nothing_matches(self):
        v = investigation_improvement_potential(
            ("unrelated cause",), "pool exhausted",
            ("traces",), ("logs",), (), ("deployment",),
        )
        assert v == 0.0

    def test_iip_deterministic(self):
        args = (("pool exhausted",), "pool exhausted at checkout",
                ("logs",), ("logs", "iops"), ("dns",), ("dns", "certs"))
        assert investigation_improvement_potential(*args) \
            == investigation_improvement_potential(*args)

    def test_worker_reduction(self):
        assert worker_reduction_score(5, 10.0) == 0.5
        assert worker_reduction_score(20, 10.0) == -1.0   # clamped
        assert worker_reduction_score(5, 0.0) == 0.0      # no baseline

    def test_evidence_acceleration(self):
        assert evidence_acceleration_score(6, 2, 8) == 0.5
        assert evidence_acceleration_score(2, 6, 8) == -0.5
        assert evidence_acceleration_score(1, 1, 0) == 0.0

    def test_false_lead_elimination(self):
        assert false_lead_elimination_score(2, 4) == 0.5
        assert false_lead_elimination_score(0, 0) == 0.0

    def test_planner_guidance(self):
        assert planner_guidance_score(("a", "b"), ("a", "c")) == 0.5
        assert planner_guidance_score((), ("a",)) == 0.0

    def test_confidence_gain(self):
        assert confidence_gain(30.0, 10.0) == 20.0
        assert confidence_gain(10.0, 30.0) == -20.0

    def test_rcas(self):
        assert root_cause_acceleration_score(60000.0, 30000) == 0.5
        assert root_cause_acceleration_score(0.0, 30000) == 0.0
        assert root_cause_acceleration_score(10000.0, 40000) == -1.0


# ---------------------------------------------------------------------------
# WS5 — Readiness gates
# ---------------------------------------------------------------------------

class TestReadinessGates:
    def test_insufficient_data_fails_closed(self):
        report = evaluate_gates(GateInputs())
        assert report["all_passed"] is False
        assert report["failed_count"] == 11
        assert report["wave3_enabled"] is False
        for g in report["gates"]:
            assert g["passed"] is False
            assert g["blocking_reason"]
            assert g["next_action"]

    def test_all_gates_pass_with_good_inputs(self):
        report = evaluate_gates(GateInputs(
            admitted_total=600,
            admitted_per_class={"kubernetes": 30, "dns": 5},
            demotion_rate_30d=0.02,
            replay_agreement_rate=0.99,
            replay_unexplained_regressions=0,
            bench_matched_mean=0.85, bench_matched_min=0.6,
            similarity_same_cause_mean=0.75,
            similarity_diff_cause_mean=0.35,
            mean_iip=0.55, mean_pgs=0.7,
            regression_share=0.02,
            false_retrieval_rate=0.05,
            max_calibration_bin_error=0.1,
            p99_latency_delta=0.01,
            failsafe_drill_completed=True,
        ))
        assert report["all_passed"] is True
        assert report["passed_count"] == 11
        # THE invariant: even a full pass never enables Wave 3.
        assert report["wave3_enabled"] is False
        assert "pending human sign-off" in report["verdict"]

    def test_single_threshold_failure_blocks(self):
        good = dict(
            admitted_total=600,
            admitted_per_class={"kubernetes": 30},
            demotion_rate_30d=0.02, replay_agreement_rate=0.99,
            replay_unexplained_regressions=0,
            bench_matched_mean=0.85, bench_matched_min=0.6,
            similarity_same_cause_mean=0.75,
            similarity_diff_cause_mean=0.35,
            mean_iip=0.55, mean_pgs=0.7, regression_share=0.02,
            false_retrieval_rate=0.05, max_calibration_bin_error=0.1,
            p99_latency_delta=0.01, failsafe_drill_completed=True,
        )
        bad = dict(good)
        bad["similarity_diff_cause_mean"] = 0.6      # violates G5
        report = evaluate_gates(GateInputs(**bad))
        assert report["all_passed"] is False
        assert "G5" in report["blocking_gates"]

    def test_g1_needs_per_class_depth(self):
        report = evaluate_gates(GateInputs(
            admitted_total=1000,
            admitted_per_class={"kubernetes": 5, "dns": 5},
        ))
        g1 = report["gates"][0]
        assert g1["gate"] == "G1"
        assert g1["passed"] is False

    def test_deterministic_report(self):
        i = GateInputs(admitted_total=100,
                        admitted_per_class={"a": 25})
        assert json.dumps(evaluate_gates(i), sort_keys=True) \
            == json.dumps(evaluate_gates(i), sort_keys=True)


# ---------------------------------------------------------------------------
# WS6 — Shadow pipeline end-to-end
# ---------------------------------------------------------------------------

class TestShadowPipeline:
    def _populate(self, tmp_path, n=3):
        astore = ArtifactStore(tmp_path / "artifacts")
        mstore = MemoryStore(tmp_path / "memory")
        for i in range(n):
            r = _enriched_result()
            r["incident_id"] = f"INC-{i}"
            a = build_artifact(r, incident_id=f"INC-{i}",
                                investigation_id=f"inv-{i}",
                                created_at=f"2026-07-0{i + 1}T00:00:00Z")
            astore.save_candidate(a)
            mstore.save(MemoryRecord.from_artifact(a))
        return astore, mstore

    def test_end_to_end_writes_reports(self, tmp_path):
        self._populate(tmp_path)
        report = run_readiness_evaluation(
            tmp_path / "artifacts", tmp_path / "memory",
            tmp_path / "out", generated_at="2026-07-10T00:00:00Z",
        )
        assert report["corpus"]["admitted_total"] == 3
        assert report["corpus"]["admitted_per_class"] == {"saturation": 3}
        assert report["readiness"]["wave3_enabled"] is False
        out = json.loads((tmp_path / "out" / "wave3_readiness.json")
                          .read_text())
        assert out == json.loads(json.dumps(report))  # JSON-safe + saved

    def test_history_is_append_only_trend(self, tmp_path):
        self._populate(tmp_path)
        for day in ("2026-07-10T00:00:00Z", "2026-07-11T00:00:00Z"):
            run_readiness_evaluation(
                tmp_path / "artifacts", tmp_path / "memory",
                tmp_path / "out", generated_at=day,
            )
        lines = (tmp_path / "out" / "readiness_history.jsonl") \
            .read_text().strip().splitlines()
        assert len(lines) == 2
        first, second = (json.loads(x) for x in lines)
        assert first["generated_at"] < second["generated_at"]
        assert first["admitted_total"] == second["admitted_total"] == 3

    def test_gates_fail_closed_on_empty_stores(self, tmp_path):
        report = run_readiness_evaluation(
            tmp_path / "a", tmp_path / "m", tmp_path / "out",
            generated_at="2026-07-10T00:00:00Z",
        )
        assert report["readiness"]["all_passed"] is False
        assert report["readiness"]["wave3_enabled"] is False

    def test_deterministic_given_same_stores_and_timestamp(self, tmp_path):
        self._populate(tmp_path)
        r1 = run_readiness_evaluation(
            tmp_path / "artifacts", tmp_path / "memory",
            tmp_path / "out1", generated_at="2026-07-10T00:00:00Z")
        r2 = run_readiness_evaluation(
            tmp_path / "artifacts", tmp_path / "memory",
            tmp_path / "out2", generated_at="2026-07-10T00:00:00Z")
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
