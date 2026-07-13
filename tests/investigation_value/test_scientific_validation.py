"""Scientific Validation Program — evaluation-only harness tests.

Coverage: ground-truth correctness, canonical evaluation record (compose +
deterministic id), E1 shadow evaluation (authoritative-unchanged fact,
safety agreement, signal quality, calibration), per-class separation,
statistics (McNemar exact/degenerate, deterministic bootstrap, effect
size), failure taxonomy, promotion gates, and the final verdict logic
including the shadow-only "insufficient evidence" path.
"""
from __future__ import annotations

import json

from sentinel_core.investigation_value.scientific_validation import (
    bootstrap_ci,
    build_report,
    canonical_evaluation_record,
    classify_failure,
    e1_shadow_evaluation,
    effect_size,
    failure_taxonomy,
    mcnemar,
    per_class_effectiveness,
    promotion_gate_assessment,
    rca_correct,
    scientific_verdict,
)


def _gt(**over):
    g = {"incident_id": "INC-1", "root_cause": "database connection pool "
         "exhaustion", "root_cause_keywords": ["connection pool", "database",
                                                "exhaustion"],
         "incident_type": "saturation", "service": "payment-service"}
    g.update(over)
    return g


def _treatment(rc="database connection pool exhaustion", **over):
    t = {
        "root_cause": rc, "confidence": 80, "citation_coverage": 0.9,
        "incident_type": "saturation",
        "_hypothesis_graph": {"hypotheses": [
            {"hypothesis_id": "h1", "name": rc, "status": "confirmed",
             "confidence": 80,
             "supporting_evidence": [{"key": "a", "weight": 1.0}],
             "refuting_evidence": []}]},
        "_investigation_validation": {
            "root_cause_verification": {"verification_status": "supports"},
            "evidence_validation": {"evidence_validation_score": 0.8},
            "confidence_reconstruction": {"evidence_confidence": 78},
            "expert_concordance": {"independent_winner": rc}},
        "_decision_intelligence": {
            "decision_arbitration": {"winner": rc},
            "decision_stability": {"stable": True},
            "decision_quality": {"overall_decision_quality": 0.85}},
        "_causal_investigation": {
            "localization": {"root_cause_service": "payment-service"}},
        "_adaptive_investigation": {},
    }
    t.update(over)
    return t


def _control(rc="database connection pool exhaustion"):
    return {"root_cause": rc, "confidence": 80}


def _record(rc=None, gt=None, ctrl_rc=None):
    rc = rc or "database connection pool exhaustion"
    return canonical_evaluation_record(
        _control(ctrl_rc or rc), _treatment(rc), gt or _gt(),
        incident_id="INC-1", model="claude")


# ---------------------------------------------------------------------------
# Ground-truth correctness
# ---------------------------------------------------------------------------

class TestRcaCorrect:
    def test_keyword_majority_true(self):
        assert rca_correct("database connection pool exhaustion", _gt()) is True

    def test_wrong_rca_false(self):
        assert rca_correct("dns misconfiguration", _gt()) is False

    def test_unlabeled_returns_none(self):
        assert rca_correct("anything", {}) is None


# ---------------------------------------------------------------------------
# Phase 1 — canonical evaluation record
# ---------------------------------------------------------------------------

class TestCanonicalRecord:
    def test_composes_all_sources(self):
        r = _record()
        assert r["authoritative"]["authoritative_unchanged"] is True
        assert r["shadow"]["independent_winner"]
        assert r["shadow"]["verification_status"] == "supports"
        assert r["ground_truth"]["treatment_correct"] is True
        assert r["incident_class"] == "database"
        for f in ("hypothesis_engine", "adaptive", "causal", "validation",
                   "decision_intelligence"):
            assert r["features"][f] is True

    def test_deterministic_id(self):
        assert _record()["record_id"] == _record()["record_id"]

    def test_authoritative_change_detected(self):
        # if treatment RCA differs from control, the contract is violated
        r = canonical_evaluation_record(
            _control("A"), _treatment("B"), _gt(), incident_id="INC-1")
        assert r["authoritative"]["authoritative_unchanged"] is False

    def test_json_safe(self):
        r = _record()
        assert r == json.loads(json.dumps(r))


# ---------------------------------------------------------------------------
# Phase 2 — E1 shadow evaluation
# ---------------------------------------------------------------------------

class TestE1:
    def test_authoritative_delta_structurally_zero(self):
        recs = [_record()]
        e1 = e1_shadow_evaluation(recs)
        d = e1["authoritative_rca_delta"]
        assert d["authoritative_unchanged"] is True
        assert d["control_correct"] == d["treatment_correct"]
        assert "structurally zero" in d["note"]

    def test_safety_agreement(self):
        e1 = e1_shadow_evaluation([_record()])
        assert e1["safety_shadow_authoritative_agreement"] == 1.0

    def test_signal_quality_counts_verified_correct(self):
        e1 = e1_shadow_evaluation([_record()])
        sq = e1["signal_quality_verification"]
        assert sq["verified_correct"] == 1
        assert sq["verified_incorrect"] == 0

    def test_empty(self):
        assert e1_shadow_evaluation([])["n"] == 0


# ---------------------------------------------------------------------------
# Phase 5 — per class
# ---------------------------------------------------------------------------

class TestPerClass:
    def test_class_separation_and_insufficiency(self):
        pc = per_class_effectiveness([_record()])
        assert "database" in pc
        assert pc["database"]["supported"] is True
        assert pc["database"]["sufficient_sample"] is False   # n=1 < 20

    def test_unknown_class_reported_separately(self):
        gt = _gt(incident_type="quantum", service="qbit", root_cause="qubit "
                 "decoherence", root_cause_keywords=["decoherence"])
        r = canonical_evaluation_record(_control("qubit decoherence"),
                                        _treatment("qubit decoherence"), gt)
        pc = per_class_effectiveness([r])
        assert "other" in pc
        assert pc["other"]["supported"] is False


# ---------------------------------------------------------------------------
# Phase 6 — statistics
# ---------------------------------------------------------------------------

class TestStatistics:
    def test_mcnemar_no_discordant(self):
        m = mcnemar(0, 0)
        assert m["significant"] is False
        assert "no measurable difference" in m["note"]

    def test_mcnemar_small_is_underpowered(self):
        m = mcnemar(1, 5)
        assert m["underpowered"] is True
        assert m["method"] == "exact_binomial"
        assert 0.0 <= m["p_value"] <= 1.0

    def test_mcnemar_large_uses_chi_square(self):
        m = mcnemar(5, 30)
        assert m["method"] == "chi_square_continuity"
        assert "chi_square_cc" in m

    def test_bootstrap_deterministic(self):
        a = bootstrap_ci([0.6, 0.7, 0.8, 0.9], seed=7)
        b = bootstrap_ci([0.6, 0.7, 0.8, 0.9], seed=7)
        assert a == b
        assert a["lo"] <= a["mean"] <= a["hi"]

    def test_bootstrap_degenerate(self):
        assert bootstrap_ci([0.5])["degenerate"] is True
        assert bootstrap_ci([])["n"] == 0

    def test_effect_size(self):
        e = effect_size([0.5, 0.5, 0.6, 0.4], [0.9, 0.8, 0.85, 0.95])
        assert e["cohens_d"] > 0
        assert e["magnitude"] in ("small", "medium", "large")

    def test_effect_size_degenerate(self):
        assert effect_size([0.5], [0.9])["cohens_d"] == "NOT_MEASURED"


# ---------------------------------------------------------------------------
# Phase 8 — failure taxonomy
# ---------------------------------------------------------------------------

class TestFailureTaxonomy:
    def test_correct_is_not_a_failure(self):
        assert classify_failure(_record()) is None

    def test_unlabeled_is_none(self):
        r = canonical_evaluation_record(_control(), _treatment(), {})
        assert classify_failure(r) is None

    def test_confidently_wrong_classified(self):
        gt = _gt(root_cause="dns outage", root_cause_keywords=["dns", "outage"])
        # treatment says pool exhaustion (wrong), verification 'supports',
        # high evidence confidence → confidently wrong
        t = _treatment("database connection pool exhaustion")
        r = canonical_evaluation_record(
            _control("database connection pool exhaustion"), t, gt)
        cat = classify_failure(r)
        assert cat in ("hypothesis", "confidence", "decision", "validation",
                        "localization", "evidence", "unknown")
        tax = failure_taxonomy([r])
        assert tax["total_failures"] == 1


# ---------------------------------------------------------------------------
# Phase 7 + verdict
# ---------------------------------------------------------------------------

class TestPromotionAndVerdict:
    def test_gates_fail_closed_on_small_corpus(self):
        e1 = e1_shadow_evaluation([_record()])
        pc = per_class_effectiveness([_record()])
        g = promotion_gate_assessment(e1, pc, corpus_total=3,
                                      regression_clean=True, replay_clean=True)
        assert g["gates"]["corpus_sufficient"] is False
        assert g["all_passed"] is False
        assert g["human_approval_required"] is True

    def test_verdict_more_shadow_evidence_when_small(self):
        recs = [_record()]
        rep = build_report(recs, corpus_total=3)
        v = rep["verdict"]
        assert v["verdict"] == "READY_AFTER_MORE_SHADOW_EVIDENCE"
        assert v["authoritative_unchanged"] is True

    def test_verdict_safety_regression_on_contract_violation(self):
        # treatment RCA differs from control ⇒ authoritative changed
        r = canonical_evaluation_record(_control("A"), _treatment("B"), _gt())
        e1 = e1_shadow_evaluation([r])
        tax = failure_taxonomy([r])
        pc = per_class_effectiveness([r])
        g = promotion_gate_assessment(e1, pc, corpus_total=3,
                                      regression_clean=True, replay_clean=True)
        v = scientific_verdict(e1, g, tax, corpus_total=3)
        assert v["verdict"] == "NOT_READY_SAFETY_REGRESSION"

    def test_report_deterministic_and_json_safe(self):
        recs = [_record()]
        a = build_report(recs, corpus_total=3)
        b = build_report(recs, corpus_total=3)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))
