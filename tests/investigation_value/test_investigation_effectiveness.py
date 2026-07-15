"""Investigation Effectiveness Program — produce-only value measurement tests.

Coverage: counterfactual comparison, per-tranche benefit attribution,
benefit score, decision attribution (differentiating vs decorative),
scientific verdict (INCONCLUSIVE under low N — fail-honest), retirement
(RETAIN_PENDING_EVIDENCE under low N), promotion (NO_TRANCHES fail-closed),
determinism, ground-truth direction.
"""
from __future__ import annotations

import json

from sentinel_core.investigation_value.investigation_effectiveness import (
    TRANCHES,
    benefit_score,
    counterfactual,
    decision_attribution,
    effectiveness_report,
    promotion_readiness,
    retirement_analysis,
    scientific_verdict,
    tranche_attribution,
)


def _result(rc="db pool exhaustion", shadow_rc=None, root_svc="db",
            sym_svc="checkout", status="supports", decisive=None, n_hyps=3,
            incident_id="INC-1"):
    shadow_rc = shadow_rc or rc
    return {
        "root_cause": rc, "confidence": 80, "incident_id": incident_id,
        "_hypothesis_graph": {"hypotheses": [
            {"name": f"h{i}", "status": "confirmed" if i == 0 else "ruled_out",
             "confidence": 80 - i * 10} for i in range(n_hyps)]},
        "_elimination_narrative": {"winner": rc, "survived_disconfirmation": True,
                                    "ruled_out": [{"name": "h1"}, {"name": "h2"}]},
        "_adaptive_investigation": {
            "uncertainty_map": {rc: ["heap"]},
            "next_best_evidence": [{"missing_evidence": ["heap_dump"]}]},
        "_causal_investigation": {
            "localization": {"root_cause_service": root_svc,
                             "symptom_service": sym_svc},
            "eliminated_chains": [{"origin": "x"}]},
        "_investigation_validation": {
            "root_cause_verification": {"verification_status": status},
            "evidence_validation": {"evidence_validation_score": 0.85,
                                    "contradicting_evidence": []},
            "expert_concordance": {"independent_winner": shadow_rc,
                                   "agreement": True},
            "confidence_reconstruction": {"evidence_confidence": 78}},
        "_decision_intelligence": {
            "decision_arbitration": {"winner": shadow_rc},
            "evidence_attribution": {"decisive_evidence": decisive or []},
            "decision_stability": {"stable": True},
            "decision_quality": {"overall_decision_quality": 0.85}},
    }


# ---------------------------------------------------------------------------
# Phase 1 — counterfactual
# ---------------------------------------------------------------------------

class TestCounterfactual:
    def test_identical_winner(self):
        cf = counterfactual(_result())
        assert cf["rca_relation"] == "identical"
        assert cf["localization_gain"] is True   # db != checkout

    def test_divergent_winner(self):
        cf = counterfactual(_result(shadow_rc="dns failure"))
        assert cf["rca_relation"] == "divergent"

    def test_validation_gate_flag(self):
        cf = counterfactual(_result(status="insufficient"))
        assert cf["validation_would_gate"] is True

    def test_no_localization_gain_when_same_service(self):
        cf = counterfactual(_result(root_svc="checkout", sym_svc="checkout"))
        assert cf["localization_gain"] is False

    def test_direction_not_measured_without_labels(self):
        assert counterfactual(_result())["ground_truth_direction"] \
            == "NOT_MEASURED"

    def test_direction_measured_with_labels(self):
        gt = {"root_cause": "dns failure",
              "root_cause_keywords": ["dns", "failure"]}
        # authoritative says db (wrong), shadow says dns (right) -> improved
        cf = counterfactual(_result(rc="db pool", shadow_rc="dns failure"), gt)
        assert cf["ground_truth_direction"] == "shadow_improved_rca"


# ---------------------------------------------------------------------------
# Phase 2 — tranche attribution
# ---------------------------------------------------------------------------

class TestTrancheAttribution:
    def test_full_benefit_levels(self):
        attr = tranche_attribution(_result(decisive=["search_oom"]))
        assert attr["hypothesis"]["benefit"] == "MAJOR"       # differential+elim+survived
        assert attr["causal"]["benefit"] == "MAJOR"           # deeper localization
        assert attr["decision"]["benefit"] == "MAJOR"         # decisive evidence
        assert attr["adaptive"]["benefit"] == "MODERATE"      # next-best evidence
        assert attr["validation"]["benefit"] == "MINOR"       # just confirms

    def test_validation_major_on_weak_conclusion(self):
        attr = tranche_attribution(_result(status="insufficient"))
        assert attr["validation"]["benefit"] == "MAJOR"
        assert attr["validation"]["differentiating"] is True

    def test_no_benefit_when_absent(self):
        attr = tranche_attribution({"root_cause": "x"})
        assert all(attr[t]["benefit"] == "NO_BENEFIT" for t in TRANCHES)

    def test_single_hypothesis_minor(self):
        attr = tranche_attribution(_result(n_hyps=1))
        assert attr["hypothesis"]["benefit"] == "MINOR"


# ---------------------------------------------------------------------------
# Phase 3 — benefit score
# ---------------------------------------------------------------------------

class TestBenefitScore:
    def test_per_tranche_metrics_with_n(self):
        bs = benefit_score([_result(decisive=["x"]) for _ in range(3)])
        assert bs["n"] == 3
        assert bs["tranche_benefit"]["causal"]["n"] == 3
        assert 0.0 <= bs["tranche_benefit"]["causal"]["value"] <= 1.0
        assert bs["localization_gain_rate"] == 1.0

    def test_divergence_rate(self):
        res = [_result(), _result(shadow_rc="other")]
        bs = benefit_score(res)
        assert bs["shadow_divergence_rate"] == 0.5


# ---------------------------------------------------------------------------
# Phase 5 — decision attribution
# ---------------------------------------------------------------------------

class TestDecisionAttribution:
    def test_consistently_matters(self):
        da = decision_attribution([_result(decisive=["x"]) for _ in range(4)])
        assert da["per_tranche"]["causal"]["verdict"] == "CONSISTENTLY_MATTERS"

    def test_decorative_when_never_differentiates(self):
        # validation that only ever confirms is non-differentiating
        da = decision_attribution([_result(status="supports")
                                   for _ in range(4)])
        assert da["per_tranche"]["validation"]["verdict"] == \
            "DECORATIVE_ON_CORPUS"


# ---------------------------------------------------------------------------
# Phase 6 — scientific verdict (fail-honest)
# ---------------------------------------------------------------------------

class TestScientificVerdict:
    def test_inconclusive_without_labels(self):
        bs = benefit_score([_result() for _ in range(3)])
        v = scientific_verdict(bs)
        assert v["rca_benefit_verdict"] == "INCONCLUSIVE"
        assert v["labeled_outcomes"] == 0

    def test_yes_when_shadow_improves_at_power(self):
        # 40 labeled where shadow is right, authoritative wrong
        gt = {"root_cause": "dns", "root_cause_keywords": ["dns"]}
        labels = {f"INC-{i}": gt for i in range(40)}
        res = [_result(rc="db pool", shadow_rc="dns", incident_id=f"INC-{i}")
               for i in range(40)]
        bs = benefit_score(res, labels)
        v = scientific_verdict(bs)
        assert v["rca_benefit_verdict"] == "YES"

    def test_no_when_shadow_worse(self):
        gt = {"root_cause": "db pool", "root_cause_keywords": ["db", "pool"]}
        labels = {f"INC-{i}": gt for i in range(40)}
        res = [_result(rc="db pool", shadow_rc="dns", incident_id=f"INC-{i}")
               for i in range(40)]
        bs = benefit_score(res, labels)
        v = scientific_verdict(bs)
        assert v["rca_benefit_verdict"] == "NO"


# ---------------------------------------------------------------------------
# Phase 7 + 8 — retirement + promotion
# ---------------------------------------------------------------------------

class TestRetirementPromotion:
    def test_retain_pending_under_low_n(self):
        da = decision_attribution([_result() for _ in range(3)])
        rt = retirement_analysis(da)
        assert all(rt["recommendations"][t]["recommendation"]
                   == "RETAIN_PENDING_EVIDENCE" for t in TRANCHES)

    def test_promotion_fail_closed(self):
        bs = benefit_score([_result() for _ in range(3)])
        v = scientific_verdict(bs)
        da = decision_attribution([_result() for _ in range(3)])
        p = promotion_readiness(v, da)
        assert p["promote"] == "NO_TRANCHES"


# ---------------------------------------------------------------------------
# Report — determinism + JSON safety
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_composes_all(self):
        rep = effectiveness_report([_result(decisive=["x"]) for _ in range(3)])
        for k in ("benefit_score", "decision_attribution", "scientific_verdict",
                   "retirement", "promotion"):
            assert k in rep

    def test_deterministic_and_json_safe(self):
        res = [_result() for _ in range(3)]
        a = effectiveness_report(res)
        b = effectiveness_report(res)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))
