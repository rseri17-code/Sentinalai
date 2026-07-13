"""Tranche 4 — evidence validation & root cause verification engine.

Coverage: evidence validation score, verification status ladder,
alternative-explanation classification, counterfactual residual,
investigation completeness, expert concordance (deterministic
re-derivation), confidence reconstruction, shadow contract, determinism.
"""
from __future__ import annotations

import copy
import json

from supervisor.validation_engine import (
    alternative_explanations,
    counterfactual_residual,
    evidence_validation,
    expert_concordance,
    investigation_completeness,
    run_validation_engine,
    verification_status,
)


def _graph(**over):
    hyps = over.pop("hypotheses", None) or [
        {"hypothesis_id": "h1", "name": "OOM kill", "status": "confirmed",
         "confidence": 78,
         "supporting_evidence": [{"key": "search_oom_logs"},
                                  {"key": "check_memory_metrics"}],
         "refuting_evidence": [], "ruled_out_reason": ""},
        {"hypothesis_id": "h2", "name": "bad deploy", "status": "ruled_out",
         "confidence": 40, "supporting_evidence": [],
         "refuting_evidence": [{"key": "get_recent_deployments"}],
         "ruled_out_reason": "refuted by deploy check"},
        {"hypothesis_id": "h3", "name": "dns issue", "status": "ruled_out",
         "confidence": 30, "supporting_evidence": [],
         "refuting_evidence": [], "ruled_out_reason": "lower net support"},
    ]
    return {"hypotheses": hyps}


def _result(**over):
    r = {
        "root_cause": "OOM kill", "confidence": 78, "raw_confidence": 82,
        "citation_coverage": 0.9,
        "citations": [{"source": "splunk:1"}, {"source": "sysdig:1"}],
        "_evidence_snapshot": {"search_oom_logs": True,
                                "check_memory_metrics": True,
                                "trace_correlation": True,
                                "cmdb_blast_radius": True,
                                "get_recent_deployments": True},
        "_critique": {"gaps": []},
        "_hypothesis_graph": _graph(),
        "_counterfactual": "Evidence most likely to change this: cap:x",
        "_adaptive_investigation": {"uncertainty_map": {"OOM kill": []},
                                     "next_best_evidence": [
                                         {"capability_id": "cap:x",
                                          "missing_evidence": ["heap_dump"]}]},
        "_causal_investigation": {"winning_chain": {"path": ["db", "checkout"]}},
    }
    r.update(over)
    return r


def _run(result=None, monkeypatch=None):
    monkeypatch.setenv("VALIDATION_ENGINE_ENABLED", "true")
    r = result or _result()
    run_validation_engine(r)
    return r


# ---------------------------------------------------------------------------
# Shadow contract
# ---------------------------------------------------------------------------

class TestShadowContract:
    def test_flag_off_no_op(self, monkeypatch):
        monkeypatch.delenv("VALIDATION_ENGINE_ENABLED", raising=False)
        r = _result()
        before = copy.deepcopy(r)
        run_validation_engine(r)
        assert r == before

    def test_additive_only_never_touches_authority(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["root_cause"] == "OOM kill"
        assert r["confidence"] == 78
        v = r["_investigation_validation"]
        for key in ("evidence_validation", "root_cause_verification",
                     "alternative_explanations", "counterfactual",
                     "investigation_completeness", "expert_concordance",
                     "confidence_reconstruction", "verification_summary"):
            assert key in v
        assert v == json.loads(json.dumps(v))          # JSON-safe

    def test_never_raises(self, monkeypatch):
        monkeypatch.setenv("VALIDATION_ENGINE_ENABLED", "true")
        run_validation_engine({})
        run_validation_engine({"_hypothesis_graph": "bad",
                                "citations": object()})

    def test_deterministic(self, monkeypatch):
        outs = []
        for _ in range(2):
            outs.append(json.dumps(
                _run(monkeypatch=monkeypatch)["_investigation_validation"],
                sort_keys=True))
        assert outs[0] == outs[1]


# ---------------------------------------------------------------------------
# Phase 1 — evidence validation
# ---------------------------------------------------------------------------

class TestEvidenceValidation:
    def test_support_contra_missing_split(self):
        v = evidence_validation(_result())
        assert v["supporting_evidence"] == ["check_memory_metrics",
                                             "search_oom_logs"]
        assert v["contradicting_evidence"] == []
        assert v["evidence_validation_score"] > 0.8

    def test_missing_from_critique_gaps(self):
        r = _result(_critique={"gaps": ["heap_profile"]})
        v = evidence_validation(r)
        assert "heap_profile" in v["missing_evidence"]

    def test_contradiction_lowers_score(self):
        g = _graph(hypotheses=[
            {"hypothesis_id": "h1", "name": "OOM", "status": "confirmed",
             "confidence": 60,
             "supporting_evidence": [{"key": "a"}],
             "refuting_evidence": [{"key": "b"}, {"key": "c"}],
             "ruled_out_reason": ""}])
        v = evidence_validation(_result(_hypothesis_graph=g,
                                         citation_coverage=0.5))
        assert v["contradicting_evidence"] == ["b", "c"]
        assert v["evidence_validation_score"] < 0.6


# ---------------------------------------------------------------------------
# Phase 2 — verification status
# ---------------------------------------------------------------------------

class TestVerificationStatus:
    def test_proves_when_well_supported(self):
        v = evidence_validation(_result())
        st = verification_status(_result(), v)
        assert st["verification_status"] == "proves"
        assert st["verified"] is True

    def test_insufficient_when_hallucination(self):
        r = _result(hallucination_risk=True)
        st = verification_status(r, evidence_validation(r))
        assert st["verification_status"] == "insufficient"
        assert st["presentable_as_root_cause"] is False

    def test_insufficient_when_gate_blocked(self):
        r = _result(_gate_post_analysis={"verdict": "block"})
        st = verification_status(r, evidence_validation(r))
        assert st["verification_status"] == "insufficient"

    def test_suggests_when_low_coverage(self):
        r = _result(citation_coverage=0.35)
        st = verification_status(r, evidence_validation(r))
        assert st["verification_status"] in ("suggests", "supports")
        # low coverage never 'proves'
        assert st["verification_status"] != "proves"

    def test_contradicts_when_refutation_dominant(self):
        g = _graph(hypotheses=[
            {"hypothesis_id": "h1", "name": "X", "status": "confirmed",
             "confidence": 40, "supporting_evidence": [{"key": "a"}],
             "refuting_evidence": [{"key": "b"}, {"key": "c"}],
             "ruled_out_reason": ""}])
        r = _result(_hypothesis_graph=g)
        st = verification_status(r, evidence_validation(r))
        assert st["verification_status"] == "contradicts"


# ---------------------------------------------------------------------------
# Phase 3 — alternative explanations
# ---------------------------------------------------------------------------

class TestAlternatives:
    def test_rejected_with_proof_vs_uncertainty(self):
        alts = alternative_explanations(_result())
        by_name = {a["hypothesis"]: a for a in alts}
        assert by_name["bad deploy"]["rejection_mode"] == \
            "rejected_with_proof"
        assert by_name["bad deploy"]["could_still_explain_symptoms"] is False
        assert by_name["dns issue"]["rejection_mode"] == \
            "rejected_with_uncertainty"
        assert by_name["dns issue"]["could_still_explain_symptoms"] is True

    def test_winner_excluded(self):
        alts = alternative_explanations(_result())
        assert "OOM kill" not in {a["hypothesis"] for a in alts}


# ---------------------------------------------------------------------------
# Phase 4 — counterfactual residual
# ---------------------------------------------------------------------------

class TestCounterfactual:
    def test_residual_zero_when_alternative_could_explain(self):
        v = evidence_validation(_result())
        alts = alternative_explanations(_result())
        cf = counterfactual_residual(_result(), v, alts)
        # dns issue (uncertainty-rejected) could still explain → residual 0
        assert cf["counterfactual_residual_score"] == 0.0
        assert "dns issue" in cf["surviving_alternatives"]

    def test_residual_high_when_all_alternatives_disproven(self):
        g = _graph(hypotheses=[
            {"hypothesis_id": "h1", "name": "OOM", "status": "confirmed",
             "confidence": 78,
             "supporting_evidence": [{"key": "a"}, {"key": "b"}],
             "refuting_evidence": [], "ruled_out_reason": ""},
            {"hypothesis_id": "h2", "name": "deploy", "status": "ruled_out",
             "confidence": 40, "supporting_evidence": [],
             "refuting_evidence": [{"key": "c"}],
             "ruled_out_reason": "refuted"}])
        r = _result(_hypothesis_graph=g)
        v = evidence_validation(r)
        cf = counterfactual_residual(r, v, alternative_explanations(r))
        assert cf["counterfactual_residual_score"] == 1.0
        assert cf["surviving_alternatives"] == []


# ---------------------------------------------------------------------------
# Phase 5 — completeness
# ---------------------------------------------------------------------------

class TestCompleteness:
    def test_categories_scored(self):
        c = investigation_completeness(_result())
        cats = c["categories_present"]
        assert cats["logs"] and cats["metrics"] and cats["topology"]
        assert cats["deployment"] and cats["traces"]
        assert 0.0 <= c["investigation_completeness_score"] <= 1.0

    def test_missing_categories_listed(self):
        r = _result(_evidence_snapshot={"search_oom_logs": True})
        c = investigation_completeness(r)
        assert "topology" in c["missing_categories"]
        assert "traces" in c["missing_categories"]


# ---------------------------------------------------------------------------
# Phase 6 — expert concordance
# ---------------------------------------------------------------------------

class TestConcordance:
    def test_agreement_when_evidence_supports_winner(self):
        c = expert_concordance(_result())
        assert c["agreement"] is True
        assert c["well_grounded"] is True
        assert c["expert_concordance_score"] == 1.0

    def test_disagreement_when_evidence_favours_other(self):
        # primary winner has NO evidence; a ruled-out one has support
        g = _graph(hypotheses=[
            {"hypothesis_id": "h1", "name": "LLM favourite",
             "status": "confirmed", "confidence": 90,
             "supporting_evidence": [], "refuting_evidence": [],
             "ruled_out_reason": ""},
            {"hypothesis_id": "h2", "name": "evidence backed",
             "status": "ruled_out", "confidence": 50,
             "supporting_evidence": [{"key": "a"}, {"key": "b"}],
             "refuting_evidence": [], "ruled_out_reason": "x"}])
        c = expert_concordance(_result(_hypothesis_graph=g))
        assert c["agreement"] is False
        assert c["independent_winner"] == "evidence backed"

    def test_not_well_grounded_when_low_coverage(self):
        c = expert_concordance(_result(citation_coverage=0.4))
        assert c["agreement"] is True
        assert c["well_grounded"] is False


# ---------------------------------------------------------------------------
# Phase 7 — confidence reconstruction + summary
# ---------------------------------------------------------------------------

class TestConfidenceReconstruction:
    def test_evidence_confidence_present_and_bounded(self, monkeypatch):
        v = _run(monkeypatch=monkeypatch)["_investigation_validation"]
        cr = v["confidence_reconstruction"]
        assert cr["raw_confidence"] == 82
        assert cr["calibrated_confidence"] == 78
        assert 0 <= cr["evidence_confidence"] <= 100
        assert cr["remaining_uncertainty"] == 100 - cr["evidence_confidence"]

    def test_summary_composes_all_signals(self, monkeypatch):
        s = _run(monkeypatch=monkeypatch)["_investigation_validation"][
            "verification_summary"]
        assert s["status"] == "proves"
        assert s["verified"] is True
        assert s["judge_agreement"] is True
        assert "evidence_confidence" in s
