"""Decision Boundary Analysis — produce-only leverage-location tests.

Coverage: per-boundary probe (present + would_change/adds), corpus analysis
(leverage rates, benefit, risk, recommendation), the two distinct answers
(highest_leverage vs safe_first_promotion), why-identical explanations,
determinism.
"""
from __future__ import annotations

import json

from sentinel_core.investigation_value.decision_boundary import (
    BOUNDARIES,
    boundary_analysis,
    boundary_probe,
)


def _result(rc="db pool exhaustion", shadow_rc=None, ev_conf=78, conf=80,
            loc="db", status="supports", decisive=None, incident_id="INC-1"):
    shadow_rc = shadow_rc or rc
    return {
        "root_cause": rc, "confidence": conf, "incident_id": incident_id,
        "_investigation_validation": {
            "root_cause_verification": {"verification_status": status},
            "expert_concordance": {"independent_winner": shadow_rc},
            "confidence_reconstruction": {"evidence_confidence": ev_conf}},
        "_decision_intelligence": {
            "decision_arbitration": {"winner": shadow_rc},
            "evidence_attribution": {"decisive_evidence": decisive or []}},
        "_causal_investigation": {
            "localization": {"root_cause_service": loc}},
    }


# ---------------------------------------------------------------------------
# Per-investigation probe
# ---------------------------------------------------------------------------

class TestBoundaryProbe:
    def test_agreeing_case_no_corrective_change(self):
        p = boundary_probe(_result(decisive=["m1"]))
        assert p["hypothesis_ranking"]["would_change"] is False
        assert p["confidence"]["would_change"] is False
        assert p["validation_gating"]["would_change"] is False
        # net-new boundaries still add
        assert p["localization"]["adds"] is True
        assert p["decision_arbitration"]["adds"] is True

    def test_divergent_ranking(self):
        p = boundary_probe(_result(shadow_rc="dns failure"))
        assert p["hypothesis_ranking"]["would_change"] is True

    def test_confidence_gap_flagged(self):
        p = boundary_probe(_result(ev_conf=40, conf=80))
        assert p["confidence"]["would_change"] is True

    def test_validation_would_gate_weak_conclusion(self):
        p = boundary_probe(_result(status="insufficient"))
        assert p["validation_gating"]["would_change"] is True

    def test_no_double_gate_when_already_gated(self):
        # baseline already gated → shadow gate is not an added change
        p = boundary_probe(_result(rc="INSUFFICIENT EVIDENCE: svc",
                                   status="insufficient"))
        assert p["validation_gating"]["would_change"] is False

    def test_absent_signals(self):
        p = boundary_probe({"root_cause": "x", "confidence": 50})
        assert p["hypothesis_ranking"]["present"] is False
        assert p["localization"]["present"] is False


# ---------------------------------------------------------------------------
# Corpus analysis
# ---------------------------------------------------------------------------

class TestBoundaryAnalysis:
    def test_all_boundaries_non_authoritative(self):
        a = boundary_analysis([_result() for _ in range(3)])
        assert all(b["authoritative_today"] is False for b in a["boundaries"])

    def test_net_new_high_benefit(self):
        a = boundary_analysis([_result(decisive=["m"]) for _ in range(3)])
        by = {b["decision_boundary"]: b for b in a["boundaries"]}
        assert by["localization"]["potential_benefit"] == "High"
        assert by["decision_arbitration"]["potential_benefit"] == "High"

    def test_ranking_no_leverage_when_agreeing(self):
        a = boundary_analysis([_result() for _ in range(3)])
        by = {b["decision_boundary"]: b for b in a["boundaries"]}
        assert by["hypothesis_ranking"]["leverage_rate"] == 0.0

    def test_ranking_leverage_when_divergent(self):
        a = boundary_analysis([_result(shadow_rc="dns"),
                               _result(shadow_rc="dns")])
        by = {b["decision_boundary"]: b for b in a["boundaries"]}
        assert by["hypothesis_ranking"]["leverage_rate"] == 1.0

    def test_two_distinct_answers(self):
        a = boundary_analysis([_result(decisive=["m"]) for _ in range(3)])
        # highest leverage is a net-new boundary; safe-first is the very-low-risk gate
        assert a["highest_leverage"] in ("decision_arbitration", "localization")
        assert a["safe_first_promotion"] == "validation_gating"

    def test_safe_first_is_lowest_risk_additive(self):
        # validation_gating has base_risk very_low — the safest additive boundary
        vg = [b for b in BOUNDARIES if b["key"] == "validation_gating"][0]
        assert vg["base_risk"] == "very_low"
        assert vg["type"] == "additive_gate"

    def test_recommendation_is_evidence_gated(self):
        # below the 30-label floor, no boundary recommends outright authority
        a = boundary_analysis([_result(shadow_rc="dns") for _ in range(3)])
        for b in a["boundaries"]:
            assert "CONTROLLED_AUTHORITY" not in b["recommendation"] \
                or b["recommendation"].startswith("SAFE_FIRST")

    def test_why_identical_explanations(self):
        a = boundary_analysis([_result() for _ in range(3)])
        joined = " ".join(a["why_identical"])
        assert "re-scores the baseline" in joined       # structural
        assert "non-authoritative" in joined            # contractual
        assert "n=3" in joined                          # evidential

    def test_empty_corpus(self):
        a = boundary_analysis([])
        assert a["n"] == 0
        assert a["highest_leverage"] == "NOT_MEASURED"

    def test_deterministic_and_json_safe(self):
        res = [_result(decisive=["m"]) for _ in range(3)]
        a = boundary_analysis(res)
        b = boundary_analysis(res)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))
