"""Tranche 5 — decision intelligence & evidence arbitration engine.

Coverage: evidence attribution (six categories + influence ranking),
decision arbitration (per-competitor deltas + topology/temporal/validation
advantage), decision stability (flip detection), sensitivity ranking,
residual uncertainty partition, explainability, decision-quality scores,
shadow contract, determinism.
"""
from __future__ import annotations

import copy
import json

from supervisor.decision_intelligence import (
    decision_arbitration,
    decision_quality,
    decision_stability,
    evidence_attribution,
    explainability,
    residual_uncertainty,
    run_decision_intelligence,
    sensitivity_analysis,
)


def _graph(**over):
    hyps = over.pop("hypotheses", None) or [
        {"hypothesis_id": "hyp:aaa", "name": "OOM kill",
         "status": "confirmed", "confidence": 78,
         "supporting_evidence": [{"key": "search_oom_logs", "weight": 1.0},
                                  {"key": "check_memory_metrics",
                                   "weight": 1.0}],
         "refuting_evidence": []},
        {"hypothesis_id": "hyp:bbb", "name": "bad deploy",
         "status": "ruled_out", "confidence": 40,
         "supporting_evidence": [{"key": "check_memory_metrics",
                                   "weight": 1.0}],
         "refuting_evidence": [{"key": "get_recent_deployments",
                                 "weight": 0.5}],
         "ruled_out_reason": "refuted"},
        {"hypothesis_id": "hyp:ccc", "name": "dns issue",
         "status": "ruled_out", "confidence": 30,
         "supporting_evidence": [], "refuting_evidence": [],
         "ruled_out_reason": "lower net support"},
    ]
    return {"hypotheses": hyps}


def _result(**over):
    r = {
        "root_cause": "OOM kill", "confidence": 78, "raw_confidence": 82,
        "_hypothesis_graph": _graph(),
        "_elimination_narrative": {"winner": "OOM kill",
                                    "survived_disconfirmation": True},
        "_counterfactual": "Evidence most likely to change this: heap_dump",
        "_evidence_snapshot": {"search_oom_logs": True,
                                "check_memory_metrics": True,
                                "trace_correlation": True,
                                "cmdb_blast_radius": True,
                                "get_recent_deployments": True,
                                "k8s_events": True},
        "_adaptive_investigation": {
            "uncertainty_map": {"OOM kill": ["heap_dump"]},
            "next_best_evidence": [{"capability_id": "cap:x",
                                     "missing_evidence": ["heap_dump"]}]},
        "_causal_investigation": {
            "anchored_hypotheses": [
                {"hypothesis": "OOM kill", "topology_possible": True},
                {"hypothesis": "dns issue", "topology_possible": False}],
            "eliminated_chains": [
                {"origin": "deploy", "hypothesis": "bad deploy",
                 "refutation": ["temporal ordering impossible"]}],
            "winning_chain": {"origin": "db", "path": ["db", "api"]}},
        "_investigation_validation": {
            "evidence_validation": {
                "supporting_evidence": ["check_memory_metrics",
                                         "search_oom_logs"],
                "contradicting_evidence": [],
                "missing_evidence": ["heap_dump"],
                "conclusive_evidence_needed": ["heap_dump"],
                "citation_coverage": 0.9,
                "evidence_validation_score": 0.94},
            "counterfactual": {"counterfactual_residual_score": 0.5,
                                "surviving_alternatives": ["dns issue"]},
            "investigation_completeness": {
                "investigation_completeness_score": 0.83},
            "expert_concordance": {"agreement": True, "well_grounded": True,
                                    "expert_concordance_score": 1.0},
            "confidence_reconstruction": {
                "raw_confidence": 82, "calibrated_confidence": 78,
                "evidence_confidence": 74, "remaining_uncertainty": 26},
            "alternative_explanations": [
                {"hypothesis": "bad deploy",
                 "rejection_mode": "rejected_with_proof"},
                {"hypothesis": "dns issue",
                 "rejection_mode": "rejected_with_uncertainty"}]},
    }
    r.update(over)
    return r


# A graph where removing one evidence item flips the winner (unstable).
def _close_race_result():
    g = _graph(hypotheses=[
        {"hypothesis_id": "hyp:aaa", "name": "A", "status": "confirmed",
         "confidence": 60,
         "supporting_evidence": [{"key": "e1", "weight": 1.0},
                                  {"key": "e2", "weight": 1.0}],
         "refuting_evidence": []},
        {"hypothesis_id": "hyp:bbb", "name": "B", "status": "ruled_out",
         "confidence": 55,
         "supporting_evidence": [{"key": "e3", "weight": 1.0}],
         "refuting_evidence": [], "ruled_out_reason": "lower"}])
    return _result(_hypothesis_graph=g,
                   _elimination_narrative={"winner": "A"})


def _run(result=None, monkeypatch=None):
    monkeypatch.setenv("DECISION_INTELLIGENCE_ENABLED", "true")
    r = result or _result()
    run_decision_intelligence(r)
    return r


# ---------------------------------------------------------------------------
# Shadow contract
# ---------------------------------------------------------------------------

class TestShadowContract:
    def test_flag_off_no_op(self, monkeypatch):
        monkeypatch.delenv("DECISION_INTELLIGENCE_ENABLED", raising=False)
        r = _result()
        before = copy.deepcopy(r)
        run_decision_intelligence(r)
        assert r == before

    def test_additive_only_never_touches_authority(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["root_cause"] == "OOM kill"
        assert r["confidence"] == 78
        di = r["_decision_intelligence"]
        for key in ("evidence_attribution", "decision_arbitration",
                     "decision_stability", "sensitivity_analysis",
                     "residual_uncertainty", "explainability",
                     "decision_quality", "decision_summary"):
            assert key in di
        assert di == json.loads(json.dumps(di))          # JSON-safe

    def test_never_raises(self, monkeypatch):
        monkeypatch.setenv("DECISION_INTELLIGENCE_ENABLED", "true")
        run_decision_intelligence({})
        run_decision_intelligence({"_hypothesis_graph": "bad",
                                    "_investigation_validation": object()})

    def test_deterministic(self, monkeypatch):
        outs = []
        for _ in range(2):
            outs.append(json.dumps(
                _run(monkeypatch=monkeypatch)["_decision_intelligence"],
                sort_keys=True))
        assert outs[0] == outs[1]


# ---------------------------------------------------------------------------
# Phase 1 — evidence attribution
# ---------------------------------------------------------------------------

class TestEvidenceAttribution:
    def test_categories_assigned(self):
        a = evidence_attribution(_result())
        cat = {x["evidence"]: x["category"] for x in a["attributions"]}
        # only the winner uses search_oom_logs → supporting
        assert cat["search_oom_logs"] == "supporting"
        # shared with a competitor → corroborating
        assert cat["check_memory_metrics"] == "corroborating"
        # in snapshot, attached to no hypothesis → contextual
        assert cat["trace_correlation"] == "contextual"
        assert cat["k8s_events"] == "contextual"

    def test_contradictory_flagged(self):
        g = _graph(hypotheses=[
            {"hypothesis_id": "hyp:aaa", "name": "A", "status": "confirmed",
             "confidence": 60,
             "supporting_evidence": [{"key": "e1", "weight": 1.0}],
             "refuting_evidence": [{"key": "bad", "weight": 1.0}]}])
        a = evidence_attribution(_result(_hypothesis_graph=g,
                                          _elimination_narrative={"winner": "A"}))
        cat = {x["evidence"]: x["category"] for x in a["attributions"]}
        assert cat["bad"] == "contradictory"

    def test_importance_ranking_ordered(self):
        a = evidence_attribution(_result())
        infl = {x["evidence"]: x["decision_influence"]
                for x in a["attributions"]}
        # highest-influence evidence ranks first
        assert a["importance_ranking"][0] == "search_oom_logs"
        assert infl["search_oom_logs"] >= infl["check_memory_metrics"]

    def test_decisive_when_removal_flips(self):
        a = evidence_attribution(_close_race_result())
        # removing e1 or e2 drops A to net 1.0 == B's 1.0; name tiebreak A wins
        # removing BOTH would flip, but single removal keeps A (tie→A).
        # Construct a definite flip: give B two supports.
        g = _graph(hypotheses=[
            {"hypothesis_id": "hyp:aaa", "name": "A", "status": "confirmed",
             "confidence": 60,
             "supporting_evidence": [{"key": "e1", "weight": 1.0},
                                      {"key": "e2", "weight": 1.0}],
             "refuting_evidence": []},
            {"hypothesis_id": "hyp:zzz", "name": "Z", "status": "ruled_out",
             "confidence": 55,
             "supporting_evidence": [{"key": "e3", "weight": 1.0},
                                      {"key": "e4", "weight": 1.0}],
             "refuting_evidence": [], "ruled_out_reason": "lower"}])
        a = evidence_attribution(_result(_hypothesis_graph=g,
                                          _elimination_narrative={"winner": "A"}))
        cat = {x["evidence"]: x["category"] for x in a["attributions"]}
        # A net=2, Z net=2 → A leads only on name tiebreak; removing e1
        # makes A=1 < Z=2 → flip → decisive
        assert cat["e1"] == "decisive"
        assert "e1" in a["decisive_evidence"]


# ---------------------------------------------------------------------------
# Phase 2 — decision arbitration
# ---------------------------------------------------------------------------

class TestDecisionArbitration:
    def test_per_competitor_record(self):
        arb = decision_arbitration(_result())
        assert arb["winner"] == "OOM kill"
        losers = {a["loser"] for a in arb["arbitrations"]}
        assert losers == {"bad deploy", "dns issue"}

    def test_topology_advantage(self):
        arb = decision_arbitration(_result())
        dns = [a for a in arb["arbitrations"] if a["loser"] == "dns issue"][0]
        assert dns["topology_advantage"] is True

    def test_temporal_and_validation_advantage(self):
        arb = decision_arbitration(_result())
        dep = [a for a in arb["arbitrations"] if a["loser"] == "bad deploy"][0]
        assert dep["temporal_advantage"] is True
        assert dep["validation_advantage"] is True
        assert dep["refuting_evidence_disadvantage"] == [
            "get_recent_deployments"]

    def test_confidence_and_margin_deltas(self):
        arb = decision_arbitration(_result())
        dns = [a for a in arb["arbitrations"] if a["loser"] == "dns issue"][0]
        assert dns["confidence_difference"] == 48
        assert dns["net_support_margin"] == 2.0


# ---------------------------------------------------------------------------
# Phase 3 — decision stability
# ---------------------------------------------------------------------------

class TestDecisionStability:
    def test_stable_decision(self):
        a = evidence_attribution(_result())
        st = decision_stability(_result(), a)
        assert st["stable"] is True
        assert st["stability_score"] == 1.0
        assert st["flips"] == []

    def test_unstable_flip_detected(self):
        r = _result(_hypothesis_graph=_graph(hypotheses=[
            {"hypothesis_id": "hyp:aaa", "name": "A", "status": "confirmed",
             "confidence": 60,
             "supporting_evidence": [{"key": "e1", "weight": 1.0},
                                      {"key": "e2", "weight": 1.0}],
             "refuting_evidence": []},
            {"hypothesis_id": "hyp:zzz", "name": "Z", "status": "ruled_out",
             "confidence": 55,
             "supporting_evidence": [{"key": "e3", "weight": 1.0},
                                      {"key": "e4", "weight": 1.0}],
             "refuting_evidence": [], "ruled_out_reason": "lower"}]),
            _elimination_narrative={"winner": "A"})
        a = evidence_attribution(r)
        st = decision_stability(r, a)
        assert st["stable"] is False
        assert "e1" in st["fragile_evidence"]
        assert any(f["new_winner"] == "Z" for f in st["flips"])
        assert st["stability_score"] < 1.0


# ---------------------------------------------------------------------------
# Phase 4 — sensitivity analysis
# ---------------------------------------------------------------------------

class TestSensitivity:
    def test_ranking_and_negligible(self):
        a = evidence_attribution(_result())
        s = sensitivity_analysis(_result(), a)
        assert s["most_influential"][0] == "search_oom_logs"
        # contextual, zero-influence evidence is negligible
        assert "trace_correlation" in s["negligible_evidence"]
        assert "k8s_events" in s["negligible_evidence"]


# ---------------------------------------------------------------------------
# Phase 5 — residual uncertainty
# ---------------------------------------------------------------------------

class TestResidualUncertainty:
    def test_partition(self):
        ru = residual_uncertainty(_result())
        assert ru["known"] == ["check_memory_metrics", "search_oom_logs"]
        assert ru["unknown"] == ["heap_dump"]
        assert ru["conflicting"] == []
        assert ru["remaining_uncertainty"] == 26
        assert ru["highest_value_next_evidence"] == ["heap_dump"]

    def test_conflicting_surfaced(self):
        r = _result()
        r["_investigation_validation"]["evidence_validation"][
            "contradicting_evidence"] = ["disk_full"]
        ru = residual_uncertainty(r)
        assert ru["conflicting"] == ["disk_full"]

    def test_assumed_when_winner_has_no_evidence(self):
        g = _graph(hypotheses=[
            {"hypothesis_id": "hyp:aaa", "name": "guesswork",
             "status": "confirmed", "confidence": 70,
             "supporting_evidence": [], "refuting_evidence": []}])
        ru = residual_uncertainty(_result(
            _hypothesis_graph=g,
            _elimination_narrative={"winner": "guesswork"}))
        assert "guesswork" in ru["assumed"]


# ---------------------------------------------------------------------------
# Phase 6 — explainability
# ---------------------------------------------------------------------------

class TestExplainability:
    def test_structured_explanation(self):
        r = _result()
        a = evidence_attribution(r)
        arb = decision_arbitration(r)
        st = decision_stability(r, a)
        ru = residual_uncertainty(r)
        ex = explainability(r, a, arb, st, ru)
        assert "OOM kill" in ex["why_this_won"]
        names = {x["hypothesis"] for x in ex["why_others_lost"]}
        assert names == {"bad deploy", "dns issue"}
        dns = [x for x in ex["why_others_lost"]
               if x["hypothesis"] == "dns issue"][0]
        assert "topology" in dns["reason"]
        assert ex["what_would_change_the_conclusion"] == ["heap_dump"]


# ---------------------------------------------------------------------------
# Phase 7 — decision quality
# ---------------------------------------------------------------------------

class TestDecisionQuality:
    def test_scores_bounded_and_present(self):
        r = _result()
        a = evidence_attribution(r)
        arb = decision_arbitration(r)
        st = decision_stability(r, a)
        ru = residual_uncertainty(r)
        q = decision_quality(r, a, arb, st, ru)
        for key in ("decision_robustness", "evidence_sufficiency",
                     "alternative_elimination", "counterfactual_strength",
                     "explanation_quality", "arbitration_completeness",
                     "overall_decision_quality"):
            assert 0.0 <= q[key] <= 1.0
        # every competitor eliminated with a concrete reason
        assert q["alternative_elimination"] == 1.0
        assert q["explanation_quality"] == 1.0

    def test_summary_composes_signals(self, monkeypatch):
        di = _run(monkeypatch=monkeypatch)["_decision_intelligence"]
        s = di["decision_summary"]
        assert s["winner"] == "OOM kill"
        assert s["stable"] is True
        assert s["remaining_uncertainty"] == 26
        assert 0.0 <= s["overall_decision_quality"] <= 1.0
