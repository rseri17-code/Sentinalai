"""90-Day Shadow Pilot — produce-only evidence-collection engine tests.

Coverage: immutable observation record (compose + deterministic id, no
wall-clock), operator-label envelope, quality scorecard (compute-only, sample
sizes, NOT_MEASURED), longitudinal trends, regression watch, production
scorecard + gatekeeper (readiness gates are the sole authority), chaos
observation, determinism.
"""
from __future__ import annotations

import json

from sentinel_core.investigation_value.readiness import GateInputs
from sentinel_core.investigation_value.scientific_validation import NOT_MEASURED
from sentinel_core.investigation_value.shadow_pilot import (
    LABEL_CORRECT,
    LABEL_INCORRECT,
    bucket_by,
    chaos_observation,
    longitudinal_trends,
    observation_record,
    production_scorecard,
    quality_scorecard,
    regression_watch,
)


def _result(rc="db pool exhaustion", conf=80, degraded=False, unavail=None):
    r = {
        "root_cause": rc, "confidence": conf, "incident_type": "saturation",
        "citation_coverage": 0.9,
        "_hypothesis_graph": {"hypotheses": [{"name": rc, "confidence": conf}]},
        "_investigation_validation": {
            "schema_version": 1,
            "root_cause_verification": {"verification_status": "supports"},
            "evidence_validation": {"evidence_validation_score": 0.85},
            "confidence_reconstruction": {"evidence_confidence": 78},
            "expert_concordance": {"expert_concordance_score": 1.0},
            "investigation_completeness": {
                "investigation_completeness_score": 0.83}},
        "_decision_intelligence": {
            "decision_arbitration": {"winner": rc},
            "decision_stability": {"stable": True},
            "decision_quality": {"overall_decision_quality": 0.85}},
        "_causal_investigation": {
            "localization": {"root_cause_service": "payment"}},
    }
    if degraded:
        r["degraded_investigation"] = True
        r["_sources_unavailable"] = unavail or [
            {"source": "knowledge_graph", "reason": "timeout"}]
    return r


def _incident():
    return {"incident_id": "INC-1", "incident_type": "saturation",
            "service": "payment", "severity": 2,
            "created_at": "2026-01-01T08:00:00Z"}


def _obs(label=None, degraded=False, rc="db pool exhaustion"):
    return observation_record(
        _result(rc=rc, degraded=degraded), _incident(),
        commit="abc123", model="opus", observed_period="2026-W09",
        replay_hash="rh1", label=label)


# ---------------------------------------------------------------------------
# Phase 1 — observation record
# ---------------------------------------------------------------------------

class TestObservationRecord:
    def test_composes_existing_outputs(self):
        o = _obs()
        c = o["core"]
        assert c["root_cause"] == "db pool exhaustion"
        assert c["evidence_validation_score"] == 0.85
        assert c["verification_status"] == "supports"
        assert c["decision_stable"] is True
        assert c["localization_service"] == "payment"
        assert o["commit"] == "abc123"
        for f in ("hypothesis_engine", "validation", "decision_intelligence"):
            assert o["feature_flags"][f] is True

    def test_deterministic_id_and_hash(self):
        assert _obs()["record_id"] == _obs()["record_id"]
        assert _obs()["determinism_hash"] == _obs()["determinism_hash"]

    def test_no_wall_clock_field(self):
        import inspect
        from sentinel_core.investigation_value import shadow_pilot
        src = inspect.getsource(shadow_pilot.observation_record)
        assert "datetime.now" not in src and "time.time" not in src

    def test_degraded_sources_captured(self):
        c = _obs(degraded=True)["core"]
        assert c["degraded_investigation"] is True
        assert c["sources_unavailable"] == [
            {"source": "knowledge_graph", "reason": "timeout"}]

    def test_json_safe(self):
        o = _obs()
        assert o == json.loads(json.dumps(o))

    def test_label_envelope(self):
        o = _obs(label={"verdict": LABEL_CORRECT,
                        "validated_root_cause": "db pool",
                        "false_positive": True})
        assert o["label"]["labeled"] is True
        assert o["label"]["verdict"] == LABEL_CORRECT
        assert o["label"]["false_positive"] is True

    def test_unlabeled_default(self):
        assert _obs()["label"]["labeled"] is False
        assert _obs()["label"]["verdict"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Phase 3 — quality scorecard
# ---------------------------------------------------------------------------

class TestQualityScorecard:
    def test_metrics_carry_sample_size(self):
        obs = [_obs(label={"verdict": LABEL_CORRECT}) for _ in range(3)]
        sc = quality_scorecard(obs, period="W09")
        assert sc["investigations"] == 3
        assert sc["labeled"] == 3
        assert sc["metrics"]["rca_accuracy"]["value"] == 1.0
        assert sc["metrics"]["rca_accuracy"]["n"] == 3
        assert sc["metrics"]["citation_coverage"]["n"] == 3

    def test_unlabeled_rca_not_measured(self):
        sc = quality_scorecard([_obs() for _ in range(3)])
        assert sc["metrics"]["rca_accuracy"]["value"] == NOT_MEASURED

    def test_source_availability_reflects_degradation(self):
        obs = [_obs(), _obs(), _obs(degraded=True), _obs()]
        sc = quality_scorecard(obs)
        assert sc["metrics"]["source_availability"]["value"] == 0.75

    def test_deterministic(self):
        obs = [_obs(label={"verdict": LABEL_CORRECT}) for _ in range(3)]
        a = json.dumps(quality_scorecard(obs), sort_keys=True)
        b = json.dumps(quality_scorecard(obs), sort_keys=True)
        assert a == b

    def test_empty(self):
        sc = quality_scorecard([])
        assert sc["investigations"] == 0
        assert sc["metrics"]["rca_accuracy"]["value"] == NOT_MEASURED


# ---------------------------------------------------------------------------
# Phase 4 — longitudinal trends
# ---------------------------------------------------------------------------

class TestTrends:
    def test_trend_direction(self):
        good = quality_scorecard(
            [_obs(label={"verdict": LABEL_CORRECT}) for _ in range(10)])
        bad = quality_scorecard(
            [_obs(rc="wrong", label={"verdict": LABEL_INCORRECT})
             for _ in range(10)])
        tr = longitudinal_trends([good, bad])
        assert tr["trends"]["rca_accuracy"]["verdict"] in (
            "degrading", "flat")
        assert tr["periods"] == 2

    def test_single_period_not_measured(self):
        sc = quality_scorecard([_obs(label={"verdict": LABEL_CORRECT})])
        tr = longitudinal_trends([sc])
        assert tr["trends"]["rca_accuracy"]["verdict"] == NOT_MEASURED

    def test_bucket_by_dimension(self):
        obs = [_obs(), _obs()]
        assert sorted(bucket_by(obs, "service")) == ["payment"]
        assert bucket_by(obs, "nonsense") == {}


# ---------------------------------------------------------------------------
# Phase 5 — regression watch
# ---------------------------------------------------------------------------

class TestRegressionWatch:
    def test_detects_accuracy_regression(self):
        base = quality_scorecard(
            [_obs(label={"verdict": LABEL_CORRECT}) for _ in range(40)])
        cur = quality_scorecard(
            [_obs(rc="wrong", label={"verdict": LABEL_INCORRECT})
             for _ in range(40)])
        rw = regression_watch(base, cur, first_period="W09", last_period="W09")
        metrics = {r["metric"] for r in rw["regressions"]}
        assert "rca_accuracy" in metrics
        acc = [r for r in rw["regressions"]
               if r["metric"] == "rca_accuracy"][0]
        assert acc["confidence"] == "high"       # n>=30
        assert acc["recommended_action"]

    def test_no_regression_when_stable(self):
        base = quality_scorecard(
            [_obs(label={"verdict": LABEL_CORRECT}) for _ in range(40)])
        rw = regression_watch(base, base)
        assert rw["regression_count"] == 0

    def test_determinism_regression_flagged(self):
        base = {"determinism": "PASS", "metrics": {}, "investigations": 5}
        cur = {"determinism": "REVIEW", "metrics": {}, "investigations": 5}
        rw = regression_watch(base, cur)
        assert any(r["metric"] == "determinism" for r in rw["regressions"])


# ---------------------------------------------------------------------------
# Phase 6 + 7 — production scorecard + gatekeeper
# ---------------------------------------------------------------------------

class TestProductionScorecard:
    def test_gates_failing_small_corpus(self):
        sc = quality_scorecard([_obs(label={"verdict": LABEL_CORRECT})])
        gi = GateInputs(admitted_total=3, admitted_per_class={"saturation": 1})
        ps = production_scorecard(sc, gi)
        assert ps["gatekeeper_verdict"] == "GATES_FAILING"
        assert ps["wave3_recommendation"] == "NOT_READY"
        assert 0.0 <= ps["pti_coverage"] <= 1.0

    def test_gates_pass_full_evidence(self):
        gi = GateInputs(
            admitted_total=600, admitted_per_class={"saturation": 25},
            demotion_rate_30d=0.0, replay_agreement_rate=0.99,
            replay_unexplained_regressions=0, bench_matched_mean=0.8,
            bench_matched_min=0.6, similarity_same_cause_mean=0.6,
            similarity_diff_cause_mean=0.2, mean_iip=0.5, mean_pgs=0.6,
            regression_share=0.0, false_retrieval_rate=0.0,
            max_calibration_bin_error=0.05, p99_latency_delta=0.0,
            failsafe_drill_completed=True)
        ps = production_scorecard(quality_scorecard([]), gi)
        assert ps["gatekeeper_verdict"] == "ALL_GATES_PASS"
        assert ps["wave3_recommendation"] == "READY"

    def test_pti_coverage_reported(self):
        obs = [_obs(label={"verdict": LABEL_CORRECT}) for _ in range(3)]
        ps = production_scorecard(quality_scorecard(obs), None)
        # PTI computed over measured dimensions; coverage in [0,1]
        assert isinstance(ps["production_trust_index"], float)
        assert 0.0 < ps["pti_coverage"] <= 1.0


# ---------------------------------------------------------------------------
# Phase 8 — chaos observation
# ---------------------------------------------------------------------------

class TestChaosObservation:
    def test_aggregates_unavailable_sources(self):
        obs = [_obs(), _obs(degraded=True),
               _obs(degraded=True,
                    rc="x")]
        obs[2]["core"]["sources_unavailable"] = [
            {"source": "splunk", "reason": "connection refused"}]
        c = chaos_observation(obs, period="W09")
        assert c["investigations"] == 3
        assert c["degraded_investigations"] == 2
        assert "knowledge_graph" in c["unavailable_by_source"]
        assert "splunk" in c["unavailable_by_source"]

    def test_empty(self):
        c = chaos_observation([])
        assert c["degraded_rate"] == NOT_MEASURED
