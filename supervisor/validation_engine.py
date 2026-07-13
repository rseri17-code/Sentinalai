"""Tranche 4 — Evidence Validation & Root Cause Verification (shadow).

A hypothesis surviving disconfirmation (Tranche 1) is still not proof.
This engine validates the conclusion before it is presented, composing
ONLY deterministic signals already on the result at analyze time:

  citations / citation_coverage / hallucination_risk  (annotate_citations)
  _hypothesis_graph / _elimination_narrative / _counterfactual  (Tranche 1)
  _adaptive_investigation                                        (Tranche 2)
  _causal_investigation                                          (Tranche 3)
  _evidence_snapshot / _gate_post_analysis / _grounding

It answers the four questions a great investigator asks of its own
conclusion — can I prove it? what would invalidate it? is it complete?
would a second investigator agree? — and produces a verification status
that stops calling unsupported explanations "root causes".

Independent verification (Phase 6) is a DETERMINISTIC cited-evidence
re-derivation, not the LLM judge (which runs later, in PersistPhase, and
is non-deterministic). It asks: restricted to only the cited evidence,
does the same hypothesis still win? This is replayable and complements —
does not replace — the downstream LLM judge.

SHADOW CONTRACT: writes only ``result['_investigation_validation']``.
Never changes root_cause / confidence / evidence routing. Flag OFF
(default) ⇒ result untouched. Deterministic: sorted iteration, fixed
weights, no clock, no randomness. Never raises past its own boundary.

Flag: VALIDATION_ENGINE_ENABLED (default OFF).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger("sentinalai.validation_engine")

# Evidence category → substrings that identify a present evidence key.
_EVIDENCE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "logs":        ("log", "oom", "error", "timeout"),
    "traces":      ("trace", "apm", "span"),
    "metrics":     ("metric", "golden", "signal", "latency", "cpu",
                     "memory", "iops", "resource"),
    "topology":    ("cmdb", "topology", "dependency", "blast"),
    "deployment":  ("deploy", "change", "diff", "git", "commit", "rollback"),
    "infrastructure": ("k8s", "pod", "node", "event", "network", "dns",
                        "host"),
}

# Verification thresholds (documented; tuned later from shadow data).
_PROVE_COVERAGE = 0.85
_SUPPORT_COVERAGE = 0.60
_SUGGEST_COVERAGE = 0.30


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _round(x: float) -> float:
    return round(float(x), 4)


# ---------------------------------------------------------------------------
# Winner extraction from the Tranche 1 hypothesis graph
# ---------------------------------------------------------------------------

def _winner(result: Mapping[str, Any]) -> dict[str, Any] | None:
    graph = result.get("_hypothesis_graph")
    if not isinstance(graph, dict):
        return None
    confirmed = [h for h in graph.get("hypotheses", [])
                 if h.get("status") == "confirmed"]
    if confirmed:
        return confirmed[0]
    hyps = graph.get("hypotheses", [])
    return max(hyps, key=lambda h: h.get("confidence", 0)) if hyps else None


# ---------------------------------------------------------------------------
# Phase 1 — Evidence Validation Score
# ---------------------------------------------------------------------------

def evidence_validation(result: Mapping[str, Any]) -> dict[str, Any]:
    """Support / contradiction / missing for the winning claim, and a
    composite Evidence Validation Score in [0, 1]."""
    winner = _winner(result)
    supporting = sorted({e.get("key", "") for e in
                         (winner or {}).get("supporting_evidence", [])
                         if e.get("key")}) if winner else []
    contradicting = sorted({e.get("key", "") for e in
                            (winner or {}).get("refuting_evidence", [])
                            if e.get("key")}) if winner else []

    # Missing: critique gaps + the winner's adaptive uncertainty.
    missing: set[str] = set()
    crit = result.get("_critique")
    if isinstance(crit, dict):
        for g in crit.get("gaps") or ():
            missing.add(str(g if isinstance(g, str) else g))
    adaptive = result.get("_adaptive_investigation")
    if isinstance(adaptive, dict) and winner:
        um = adaptive.get("uncertainty_map") or {}
        missing |= set(um.get(winner.get("name", ""), []))

    # Conclusively-proving evidence: the next-best acquisition the
    # adaptive advisor would run (highest expected discrimination).
    conclusive: list[str] = []
    if isinstance(adaptive, dict):
        nbe = adaptive.get("next_best_evidence") or []
        if nbe:
            conclusive = list(nbe[0].get("missing_evidence", []))

    s, c, m = len(supporting), len(contradicting), len(missing)
    coverage = float(result.get("citation_coverage", 0.0) or 0.0)
    # blend citation coverage (grounding) with support/contra balance
    balance = (s / (s + c + m)) if (s + c + m) else 0.0
    score = _round(0.6 * coverage + 0.4 * balance)

    return {
        "supporting_evidence": supporting,
        "contradicting_evidence": contradicting,
        "missing_evidence": sorted(missing),
        "conclusive_evidence_needed": sorted(conclusive),
        "citation_coverage": _round(coverage),
        "evidence_validation_score": score,
    }


# ---------------------------------------------------------------------------
# Phase 2 — Root Cause Verification status
# ---------------------------------------------------------------------------

def verification_status(
    result: Mapping[str, Any], validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Map to proves / supports / suggests / contradicts / insufficient.
    The engine stops calling unsupported explanations 'root causes'."""
    coverage = validation["citation_coverage"]
    score = validation["evidence_validation_score"]
    contradicted = len(validation["contradicting_evidence"])
    hallucination = bool(result.get("hallucination_risk"))
    gate = result.get("_gate_post_analysis") or {}
    blocked = isinstance(gate, dict) and gate.get("verdict") == "block"

    if blocked or hallucination:
        status = "insufficient"
    elif contradicted and contradicted >= len(
            validation["supporting_evidence"]):
        status = "contradicts"
    elif coverage >= _PROVE_COVERAGE and score >= _PROVE_COVERAGE \
            and not contradicted:
        status = "proves"
    elif coverage >= _SUPPORT_COVERAGE and score >= _SUPPORT_COVERAGE:
        status = "supports"
    elif coverage >= _SUGGEST_COVERAGE or score >= _SUGGEST_COVERAGE:
        status = "suggests"
    else:
        status = "insufficient"

    verified = status in ("proves", "supports")
    return {
        "verification_status": status,
        "verified": verified,
        "presentable_as_root_cause": verified,
        "qualifier": {
            "proves": "", "supports": "SUPPORTED",
            "suggests": "SUGGESTED — not proven",
            "contradicts": "CONTRADICTED by evidence",
            "insufficient": "INSUFFICIENT EVIDENCE",
        }[status],
    }


# ---------------------------------------------------------------------------
# Phase 3 — Alternative explanation verification
# ---------------------------------------------------------------------------

def alternative_explanations(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every ruled-out hypothesis: rejected with proof (had refuting
    evidence) or with uncertainty (only lost on net support)."""
    graph = result.get("_hypothesis_graph")
    if not isinstance(graph, dict):
        return []
    winner = _winner(result)
    winner_id = (winner or {}).get("hypothesis_id")
    out = []
    for h in graph.get("hypotheses", []):
        if h.get("hypothesis_id") == winner_id:
            continue
        if h.get("status") != "ruled_out":
            continue
        refuting = sorted({e.get("key", "") for e in
                           h.get("refuting_evidence", []) if e.get("key")})
        if refuting:
            mode, could_explain = "rejected_with_proof", False
        else:
            mode, could_explain = "rejected_with_uncertainty", True
        out.append({
            "hypothesis": h.get("name", ""),
            "rejection_mode": mode,
            "could_still_explain_symptoms": could_explain,
            "refuting_evidence": refuting,
            "ruled_out_reason": h.get("ruled_out_reason", ""),
        })
    return sorted(out, key=lambda x: x["hypothesis"])


# ---------------------------------------------------------------------------
# Phase 4 — Counterfactual Residual Score
# ---------------------------------------------------------------------------

def counterfactual_residual(
    result: Mapping[str, Any], validation: Mapping[str, Any],
    alternatives: list[dict[str, Any]],
) -> dict[str, Any]:
    """If the winner were false: which cited evidence becomes unexplained?
    Evidence the winner explains that NO surviving alternative could
    explain is 'load-bearing' — higher residual = more defensible."""
    supporting = set(validation["supporting_evidence"])
    # evidence any still-plausible alternative could also explain
    alt_explainable: set[str] = set()
    for a in alternatives:
        if a["could_still_explain_symptoms"]:
            # an uncertainty-rejected alt shares the symptom evidence
            alt_explainable |= supporting
    load_bearing = sorted(supporting - alt_explainable)
    unexplained_if_false = load_bearing
    total = len(supporting) or 1
    residual = _round(len(load_bearing) / total)
    causal = result.get("_causal_investigation") or {}
    chain_collapses = bool(
        isinstance(causal, dict)
        and (causal.get("winning_chain") or {}).get("path"))
    return {
        "counterfactual_statement": str(result.get("_counterfactual", "")),
        "evidence_unexplained_if_false": unexplained_if_false,
        "surviving_alternatives": sorted(
            a["hypothesis"] for a in alternatives
            if a["could_still_explain_symptoms"]),
        "causal_chain_collapses": chain_collapses,
        "counterfactual_residual_score": residual,
    }


# ---------------------------------------------------------------------------
# Phase 5 — Investigation Completeness Score
# ---------------------------------------------------------------------------

def investigation_completeness(result: Mapping[str, Any]) -> dict[str, Any]:
    snap = result.get("_evidence_snapshot")
    keys = {str(k).lower() for k, v in snap.items() if v} \
        if isinstance(snap, dict) else set()
    present: dict[str, bool] = {}
    for cat, subs in sorted(_EVIDENCE_CATEGORIES.items()):
        present[cat] = any(any(s in k for s in subs) for k in keys)
    missing = sorted(c for c, ok in present.items() if not ok)
    score = _round(sum(present.values()) / len(present)) if present else 0.0
    return {
        "categories_present": {c: present[c] for c in sorted(present)},
        "missing_categories": missing,
        "investigation_completeness_score": score,
    }


# ---------------------------------------------------------------------------
# Phase 6 — Expert Concordance (deterministic cited-evidence re-derivation)
# ---------------------------------------------------------------------------

def expert_concordance(result: Mapping[str, Any]) -> dict[str, Any]:
    """Independent second opinion, deterministic and replayable.

    Re-ranks the hypotheses by STRUCTURED-EVIDENCE net support alone
    (count of supporting minus refuting evidence attached during the
    investigation), deliberately ignoring the LLM base confidence the
    primary selection used. If this evidence-only re-derivation picks the
    same hypothesis, a second investigator working from the same evidence
    would agree. Whether that agreement is well-grounded is qualified by
    citation coverage. Complements — does not replace — the downstream
    LLM judge (which runs later, in PersistPhase, non-deterministically).
    """
    graph = result.get("_hypothesis_graph")
    winner = _winner(result)
    if not isinstance(graph, dict) or winner is None:
        return {"independent_winner": "", "primary_winner": "",
                "agreement": None, "expert_concordance_score": 0.0,
                "reason": "no hypothesis graph",
                "method": "deterministic_evidence_net_support_rederivation"}

    scored = []
    for h in graph.get("hypotheses", []):
        sup = len(h.get("supporting_evidence", []))
        ref = len(h.get("refuting_evidence", []))
        # net support desc, then name asc — fully deterministic
        scored.append((-(sup - ref), str(h.get("name", "")), h))
    scored.sort()
    independent = scored[0][2] if scored else winner
    agree = independent.get("hypothesis_id") == winner.get("hypothesis_id")
    coverage = float(result.get("citation_coverage", 0.0) or 0.0)
    well_grounded = agree and coverage >= _SUPPORT_COVERAGE
    return {
        "independent_winner": independent.get("name", ""),
        "primary_winner": winner.get("name", ""),
        "agreement": bool(agree),
        "well_grounded": bool(well_grounded),
        "expert_concordance_score": 1.0 if agree else 0.0,
        "reason": ("evidence-only re-derivation selected the same hypothesis"
                    + ("" if well_grounded else
                       "; citation coverage below support floor")
                    if agree else
                    "evidence net-support favours a different hypothesis "
                    "than the primary selection — conclusion leans on the "
                    "LLM's prior, not the collected evidence"),
        "method": "deterministic_evidence_net_support_rederivation",
    }


# ---------------------------------------------------------------------------
# Phase 7 — Confidence Reconstruction
# ---------------------------------------------------------------------------

def reconstruct_confidence(
    result: Mapping[str, Any],
    validation: Mapping[str, Any],
    counterfactual: Mapping[str, Any],
    concordance: Mapping[str, Any],
    completeness: Mapping[str, Any],
) -> dict[str, Any]:
    """Evidence-derived confidence (SHADOW — does not overwrite result)."""
    raw = int(result.get("raw_confidence",
                         result.get("confidence", 0)) or 0)
    calibrated = int(result.get("confidence", 0) or 0)

    ev_score = validation["evidence_validation_score"]
    residual = counterfactual["counterfactual_residual_score"]
    concord = concordance["expert_concordance_score"]
    complete = completeness["investigation_completeness_score"]
    contra_penalty = 0.1 * len(validation["contradicting_evidence"])

    evidence_conf = max(0.0, min(1.0,
        0.35 * ev_score + 0.20 * residual + 0.20 * concord
        + 0.25 * complete - contra_penalty))
    evidence_confidence = int(round(evidence_conf * 100))
    remaining_uncertainty = 100 - evidence_confidence
    return {
        "raw_confidence": raw,
        "calibrated_confidence": calibrated,
        "evidence_confidence": evidence_confidence,
        "remaining_uncertainty": remaining_uncertainty,
        "components": {
            "evidence_validation": ev_score,
            "counterfactual_residual": residual,
            "expert_concordance": concord,
            "completeness": complete,
            "contradiction_penalty": _round(contra_penalty),
        },
    }


# ---------------------------------------------------------------------------
# Entry point (shadow)
# ---------------------------------------------------------------------------

def run_validation_engine(result: dict[str, Any]) -> None:
    """Attach ``_investigation_validation`` shadow metadata. No-op unless
    VALIDATION_ENGINE_ENABLED. Never raises. Never changes root_cause /
    confidence."""
    if not _flag("VALIDATION_ENGINE_ENABLED"):
        return
    try:
        validation = evidence_validation(result)
        status = verification_status(result, validation)
        alternatives = alternative_explanations(result)
        counterfactual = counterfactual_residual(
            result, validation, alternatives)
        completeness = investigation_completeness(result)
        concordance = expert_concordance(result)
        confidence = reconstruct_confidence(
            result, validation, counterfactual, concordance, completeness)

        result["_investigation_validation"] = {
            "evidence_validation": validation,
            "root_cause_verification": status,
            "alternative_explanations": alternatives,
            "counterfactual": counterfactual,
            "investigation_completeness": completeness,
            "expert_concordance": concordance,
            "confidence_reconstruction": confidence,
            "verification_summary": {
                "status": status["verification_status"],
                "verified": status["verified"],
                "evidence_confidence": confidence["evidence_confidence"],
                "remaining_uncertainty": confidence["remaining_uncertainty"],
                "judge_agreement": concordance["agreement"],
            },
        }
    except Exception as exc:
        logger.warning(
            "validation_engine.failed error_type=%s error=%s",
            type(exc).__name__, exc)


__all__ = [
    "alternative_explanations", "counterfactual_residual",
    "evidence_validation", "expert_concordance",
    "investigation_completeness", "reconstruct_confidence",
    "run_validation_engine", "verification_status",
]
