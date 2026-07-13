"""Tranche 5 — Decision Intelligence & Evidence Arbitration (shadow).

Tranches 1-4 taught AnalyzePhase to think: generate hypotheses, eliminate
competitors, acquire evidence adaptively, reason over topology, and
validate the conclusion. What they do NOT yet produce is a *defensible
decision* — the justification a principal SRE or expert witness would give
when asked "why THIS root cause, and not any of the others?".

This engine converts a validated root cause into a defensible investigative
decision. It answers, deterministically:

  * Why did this hypothesis win, and why did the others lose?
  * Which evidence was decisive, and which never mattered?
  * How stable is the decision — would it flip if one fact were removed?
  * How much uncertainty remains, and of what kind?
  * What single piece of evidence would most increase confidence?
  * Would another equally rational investigator reach the same conclusion?

It COMPOSES signals already present on the analyze-time result — it never
recomputes support, refutation, confidence, causal, or validation:

  _hypothesis_graph          (T1) — support/refute weights, per-hyp nets
  _elimination_narrative     (T1) — winner identity, net support
  _adaptive_investigation    (T2) — unknowns, highest-value next evidence
  _causal_investigation      (T3) — topology / temporal advantage per hyp
  _investigation_validation  (T4) — verification, load-bearing evidence,
                                    residual uncertainty, concordance

SHADOW CONTRACT: writes only ``result['_decision_intelligence']``. Never
changes root_cause / confidence / evidence routing. Flag OFF (default) ⇒
result untouched. Deterministic: sorted iteration, weighted net support,
no clock, no randomness. Never raises past its own boundary.

Flag: DECISION_INTELLIGENCE_ENABLED (default OFF).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger("sentinalai.decision_intelligence")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _round(x: float) -> float:
    return round(float(x), 4)


# ---------------------------------------------------------------------------
# Shared: hypotheses, weighted net support, winner
# ---------------------------------------------------------------------------

def _hypotheses(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    graph = result.get("_hypothesis_graph")
    if not isinstance(graph, dict):
        return []
    return [h for h in graph.get("hypotheses", []) if isinstance(h, dict)]


def _ev_keys(items: Any) -> list[tuple[str, float]]:
    """(key, weight) for a supporting/refuting evidence list."""
    out = []
    for e in items or ():
        if isinstance(e, dict) and e.get("key"):
            out.append((str(e["key"]), float(e.get("weight", 1.0) or 0.0)))
    return out


def _net(h: Mapping[str, Any], drop: str = "") -> float:
    """Weighted net support = Σ support − Σ refute, optionally dropping one
    evidence key (used by stability / sensitivity)."""
    sup = sum(w for k, w in _ev_keys(h.get("supporting_evidence")) if k != drop)
    ref = sum(w for k, w in _ev_keys(h.get("refuting_evidence")) if k != drop)
    return sup - ref


def _winner(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Prefer the winner T1 already named; fall back to confirmed / highest
    confidence. Never recomputes the selection."""
    hyps = _hypotheses(result)
    if not hyps:
        return None
    narrative = result.get("_elimination_narrative")
    if isinstance(narrative, dict) and narrative.get("winner"):
        wname = str(narrative["winner"])
        for h in hyps:
            if str(h.get("name", "")) == wname:
                return h
    confirmed = [h for h in hyps if h.get("status") == "confirmed"]
    if confirmed:
        return confirmed[0]
    return max(hyps, key=lambda h: h.get("confidence", 0))


def _argmax_net(hyps: list[dict[str, Any]], drop: str = "") -> str:
    """hypothesis_id with the highest net support (name asc tiebreak)."""
    ranked = sorted(hyps, key=lambda h: (-_net(h, drop), str(h.get("name", ""))))
    return str(ranked[0].get("hypothesis_id", "")) if ranked else ""


# ---------------------------------------------------------------------------
# Phase 1 — Evidence Attribution
# ---------------------------------------------------------------------------

def evidence_attribution(result: Mapping[str, Any]) -> dict[str, Any]:
    """Classify every evidence item by its contribution to the decision:
    decisive / supporting / corroborating / contextual / redundant /
    contradictory — and rank by decision influence."""
    hyps = _hypotheses(result)
    winner = _winner(result)
    if not hyps or winner is None:
        return {"attributions": [], "importance_ranking": [],
                "decisive_evidence": []}
    win_id = str(winner.get("hypothesis_id", ""))
    win_support = {k for k, _ in _ev_keys(winner.get("supporting_evidence"))}
    win_refute = {k for k, _ in _ev_keys(winner.get("refuting_evidence"))}

    # which hypotheses each key supports (for discrimination judgement)
    supporters: dict[str, set[str]] = {}
    for h in hyps:
        hid = str(h.get("hypothesis_id", ""))
        for k, _ in _ev_keys(h.get("supporting_evidence")):
            supporters.setdefault(k, set()).add(hid)

    base_margin = _margin(hyps, win_id)
    all_keys: set[str] = set(supporters) | win_refute
    # contextual: present in the snapshot but attached to no hypothesis
    snap = result.get("_evidence_snapshot")
    snap_keys = {str(k) for k, v in snap.items() if v} \
        if isinstance(snap, dict) else set()
    contextual_keys = sorted(snap_keys - all_keys)

    attributions = []
    for k in sorted(all_keys):
        infl = _round(base_margin - _margin(hyps, win_id, drop=k))
        flips = _argmax_net(hyps, drop=k) != win_id if k in win_support else False
        sups = supporters.get(k, set())
        if k in win_refute:
            category = "contradictory"
        elif k in win_support:
            if flips:
                category = "decisive"
            elif len(sups) == 1:
                category = "supporting"          # only the winner uses it
            elif len(sups) >= len(hyps):
                category = "redundant"           # supports everyone equally
            else:
                category = "corroborating"       # shared but still net-positive
        else:
            category = "corroborating"           # supports a consistent alt
        attributions.append({
            "evidence": k, "category": category,
            "decision_influence": infl,
            "supports_winner": k in win_support,
            "hypotheses_supported": len(sups),
        })

    for k in contextual_keys:
        attributions.append({
            "evidence": k, "category": "contextual",
            "decision_influence": 0.0,
            "supports_winner": False, "hypotheses_supported": 0,
        })

    ranking = sorted(
        attributions,
        key=lambda a: (-a["decision_influence"], a["evidence"]))
    return {
        "attributions": sorted(attributions, key=lambda a: a["evidence"]),
        "importance_ranking": [a["evidence"] for a in ranking],
        "decisive_evidence": sorted(
            a["evidence"] for a in attributions
            if a["category"] == "decisive"),
    }


def _margin(hyps: list[dict[str, Any]], win_id: str, drop: str = "") -> float:
    """Winner net minus the best competitor net (the decision's lead)."""
    win = next((h for h in hyps
                if str(h.get("hypothesis_id", "")) == win_id), None)
    if win is None:
        return 0.0
    others = [h for h in hyps if str(h.get("hypothesis_id", "")) != win_id]
    best_other = max((_net(h, drop) for h in others), default=0.0)
    return _net(win, drop) - best_other


# ---------------------------------------------------------------------------
# Phase 2 — Decision Arbitration
# ---------------------------------------------------------------------------

def decision_arbitration(result: Mapping[str, Any]) -> dict[str, Any]:
    """For every competing hypothesis: why the winner beat it."""
    hyps = _hypotheses(result)
    winner = _winner(result)
    if not hyps or winner is None:
        return {"winner": "", "arbitrations": []}
    win_id = str(winner.get("hypothesis_id", ""))

    topo = _topology_index(result)
    temporal = _temporal_eliminated(result)
    verified_alt = _rejected_with_proof(result)

    win_sup = {k for k, _ in _ev_keys(winner.get("supporting_evidence"))}
    win_ref = {k for k, _ in _ev_keys(winner.get("refuting_evidence"))}

    arbitrations = []
    for h in hyps:
        hid = str(h.get("hypothesis_id", ""))
        if hid == win_id:
            continue
        name = str(h.get("name", ""))
        h_sup = {k for k, _ in _ev_keys(h.get("supporting_evidence"))}
        h_ref = {k for k, _ in _ev_keys(h.get("refuting_evidence"))}
        arbitrations.append({
            "loser": name,
            "supporting_evidence_advantage": sorted(win_sup - h_sup),
            "refuting_evidence_disadvantage": sorted(h_ref - win_ref),
            "topology_advantage": bool(
                topo.get(str(winner.get("name", "")), True)
                and not topo.get(name, True)),
            "temporal_advantage": name in temporal,
            "validation_advantage": name in verified_alt,
            "net_support_margin": _round(_net(winner) - _net(h)),
            "confidence_difference": int(winner.get("confidence", 0)
                                          or 0) - int(h.get("confidence", 0)
                                                       or 0),
        })
    return {
        "winner": str(winner.get("name", "")),
        "arbitrations": sorted(arbitrations, key=lambda a: a["loser"]),
    }


def _topology_index(result: Mapping[str, Any]) -> dict[str, bool]:
    ci = result.get("_causal_investigation")
    out: dict[str, bool] = {}
    if isinstance(ci, dict):
        for a in ci.get("anchored_hypotheses", []) or ():
            if isinstance(a, dict) and a.get("hypothesis"):
                out[str(a["hypothesis"])] = bool(a.get("topology_possible", True))
    return out


def _temporal_eliminated(result: Mapping[str, Any]) -> set[str]:
    """Hypothesis names whose causal chain was eliminated on temporal
    grounds (their cause post-dates the symptom)."""
    ci = result.get("_causal_investigation")
    names: set[str] = set()
    if not isinstance(ci, dict):
        return names
    # map anchor origin -> hypothesis names is lossy; use chain refutation text
    for c in ci.get("eliminated_chains", []) or ():
        if isinstance(c, dict) and any(
                "temporal" in str(x) for x in c.get("refutation", []) or ()):
            names.add(str(c.get("hypothesis", c.get("origin", ""))))
    return names


def _rejected_with_proof(result: Mapping[str, Any]) -> set[str]:
    v = result.get("_investigation_validation")
    out: set[str] = set()
    if isinstance(v, dict):
        for a in v.get("alternative_explanations", []) or ():
            if isinstance(a, dict) and \
                    a.get("rejection_mode") == "rejected_with_proof":
                out.add(str(a.get("hypothesis", "")))
    return out


# ---------------------------------------------------------------------------
# Phase 3 — Decision Stability
# ---------------------------------------------------------------------------

def decision_stability(
    result: Mapping[str, Any], attribution: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove each decisive evidence item; does the winner change?"""
    hyps = _hypotheses(result)
    winner = _winner(result)
    if not hyps or winner is None:
        return {"stable": True, "flips": [], "decisive_count": 0,
                "stability_score": 1.0}
    win_id = str(winner.get("hypothesis_id", ""))
    decisive = attribution.get("decisive_evidence", []) or []
    flips = []
    for k in sorted(decisive):
        if _argmax_net(hyps, drop=k) != win_id:
            flips.append({"removed_evidence": k,
                          "new_winner": _name_of(hyps,
                                                 _argmax_net(hyps, drop=k))})
    # also probe every supporting key of the winner for robustness
    tested = {k for k, _ in _ev_keys(winner.get("supporting_evidence"))}
    fragile = sorted(k for k in tested if _argmax_net(hyps, drop=k) != win_id)
    stable = not fragile
    score = _round(1.0 - (len(fragile) / len(tested))) if tested else 1.0
    return {
        "stable": stable,
        "flips": sorted(flips, key=lambda f: f["removed_evidence"]),
        "fragile_evidence": fragile,
        "decisive_count": len(fragile),
        "stability_score": score,
    }


def _name_of(hyps: list[dict[str, Any]], hid: str) -> str:
    for h in hyps:
        if str(h.get("hypothesis_id", "")) == hid:
            return str(h.get("name", ""))
    return ""


# ---------------------------------------------------------------------------
# Phase 4 — Sensitivity Analysis
# ---------------------------------------------------------------------------

def sensitivity_analysis(
    result: Mapping[str, Any], attribution: Mapping[str, Any],
) -> dict[str, Any]:
    """Rank evidence by how much it contributes to the decision margin."""
    attrs = attribution.get("attributions", []) or []
    scored = [{"evidence": a["evidence"],
               "decision_influence": a["decision_influence"],
               "category": a["category"]}
              for a in attrs]
    ranked = sorted(scored,
                    key=lambda s: (-s["decision_influence"], s["evidence"]))
    most = [s["evidence"] for s in ranked if s["decision_influence"] > 0]
    negligible = sorted(s["evidence"] for s in scored
                        if s["decision_influence"] <= 0.0
                        and s["category"] != "contradictory")
    return {
        "ranked_by_influence": ranked,
        "most_influential": most[:5],
        "negligible_evidence": negligible,
    }


# ---------------------------------------------------------------------------
# Phase 5 — Residual Uncertainty
# ---------------------------------------------------------------------------

def residual_uncertainty(result: Mapping[str, Any]) -> dict[str, Any]:
    """Partition the epistemic state: known / unknown / assumed /
    unverified / conflicting, and quantify remaining uncertainty."""
    winner = _winner(result)
    val = result.get("_investigation_validation")
    ev_val = val.get("evidence_validation", {}) if isinstance(val, dict) else {}

    known = sorted(ev_val.get("supporting_evidence", []) or [])
    conflicting = sorted(ev_val.get("contradicting_evidence", []) or [])
    unknown = sorted(ev_val.get("missing_evidence", []) or [])

    # assumed: hypotheses carrying confidence with no attached evidence
    assumed = []
    if winner is not None and not _ev_keys(winner.get("supporting_evidence")):
        assumed.append(str(winner.get("name", "")))
    # unverified: structured support not backed by a citation grounding pass
    coverage = float(ev_val.get("citation_coverage", 0.0) or 0.0)
    unverified = known if coverage < 0.6 else []

    remaining = 100 - _evidence_confidence(result)
    return {
        "known": known,
        "unknown": unknown,
        "assumed": sorted(assumed),
        "unverified": sorted(unverified),
        "conflicting": conflicting,
        "remaining_uncertainty": remaining,
        "highest_value_next_evidence": _next_best(result),
    }


def _evidence_confidence(result: Mapping[str, Any]) -> int:
    val = result.get("_investigation_validation")
    if isinstance(val, dict):
        cr = val.get("confidence_reconstruction", {})
        if isinstance(cr, dict) and "evidence_confidence" in cr:
            return int(cr["evidence_confidence"])
    return int(result.get("confidence", 0) or 0)


def _next_best(result: Mapping[str, Any]) -> list[str]:
    adaptive = result.get("_adaptive_investigation")
    if isinstance(adaptive, dict):
        nbe = adaptive.get("next_best_evidence") or []
        if nbe and isinstance(nbe[0], dict):
            return sorted(nbe[0].get("missing_evidence", []) or [])
    val = result.get("_investigation_validation")
    if isinstance(val, dict):
        ev = val.get("evidence_validation", {})
        if isinstance(ev, dict):
            return sorted(ev.get("conclusive_evidence_needed", []) or [])
    return []


# ---------------------------------------------------------------------------
# Phase 6 — Explainability
# ---------------------------------------------------------------------------

def explainability(
    result: Mapping[str, Any], attribution: Mapping[str, Any],
    arbitration: Mapping[str, Any], stability: Mapping[str, Any],
    residual: Mapping[str, Any],
) -> dict[str, Any]:
    winner = _winner(result)
    wname = str((winner or {}).get("name", ""))
    decisive = attribution.get("decisive_evidence", []) or []
    negligible = [a["evidence"] for a in attribution.get("attributions", [])
                  if a["category"] in ("redundant", "contextual")]

    why_won = (
        f"{wname} carried the strongest weighted evidence net"
        + (f", decisively on {', '.join(decisive)}" if decisive else "")
        + (" and survived every disconfirmation probe."
           if stability.get("stable") else
           " but the decision is fragile — see stability.")
    ) if wname else "No winning hypothesis resolved."

    why_others_lost = []
    for a in arbitration.get("arbitrations", []) or ():
        reasons = []
        if a["topology_advantage"]:
            reasons.append("topology-impossible origin")
        if a["temporal_advantage"]:
            reasons.append("temporally ruled out")
        if a["validation_advantage"]:
            reasons.append("refuted by evidence")
        if a["refuting_evidence_disadvantage"]:
            reasons.append("carried refuting evidence "
                           + f"({', '.join(a['refuting_evidence_disadvantage'])})")
        if not reasons:
            reasons.append(f"lower net support (margin {a['net_support_margin']})")
        why_others_lost.append({"hypothesis": a["loser"],
                                 "reason": "; ".join(reasons)})

    return {
        "why_this_won": why_won,
        "why_others_lost": sorted(why_others_lost,
                                  key=lambda x: x["hypothesis"]),
        "evidence_that_mattered": sorted(decisive),
        "evidence_that_never_mattered": sorted(set(negligible)),
        "what_would_change_the_conclusion": residual.get(
            "highest_value_next_evidence", []),
    }


# ---------------------------------------------------------------------------
# Phase 7 — Decision Quality
# ---------------------------------------------------------------------------

def decision_quality(
    result: Mapping[str, Any], attribution: Mapping[str, Any],
    arbitration: Mapping[str, Any], stability: Mapping[str, Any],
    residual: Mapping[str, Any],
) -> dict[str, Any]:
    val = result.get("_investigation_validation")
    ev_val = val.get("evidence_validation", {}) if isinstance(val, dict) else {}
    cf = val.get("counterfactual", {}) if isinstance(val, dict) else {}
    comp = val.get("investigation_completeness", {}) \
        if isinstance(val, dict) else {}

    robustness = float(stability.get("stability_score", 0.0))
    sufficiency = _round(
        0.5 * float(ev_val.get("evidence_validation_score", 0.0) or 0.0)
        + 0.5 * float(comp.get("investigation_completeness_score", 0.0) or 0.0))
    arbs = arbitration.get("arbitrations", []) or []
    eliminated = sum(1 for a in arbs if a["topology_advantage"]
                     or a["temporal_advantage"] or a["validation_advantage"]
                     or a["refuting_evidence_disadvantage"])
    alt_elimination = _round(eliminated / len(arbs)) if arbs else 1.0
    cf_strength = float(cf.get("counterfactual_residual_score", 0.0) or 0.0)
    # explanation quality: fraction of losers with a concrete (non-margin) reason
    concrete = sum(1 for a in arbs if a["topology_advantage"]
                   or a["temporal_advantage"] or a["validation_advantage"]
                   or a["refuting_evidence_disadvantage"])
    explanation_quality = _round(concrete / len(arbs)) if arbs else 1.0
    arbitration_completeness = 1.0 if arbs or not _hypotheses(result) else 0.0

    overall = _round((robustness + sufficiency + alt_elimination
                      + cf_strength + explanation_quality) / 5.0)
    return {
        "decision_robustness": _round(robustness),
        "evidence_sufficiency": sufficiency,
        "alternative_elimination": alt_elimination,
        "counterfactual_strength": _round(cf_strength),
        "explanation_quality": explanation_quality,
        "arbitration_completeness": _round(arbitration_completeness),
        "overall_decision_quality": overall,
    }


# ---------------------------------------------------------------------------
# Entry point (shadow)
# ---------------------------------------------------------------------------

def run_decision_intelligence(result: dict[str, Any]) -> None:
    """Attach ``_decision_intelligence`` shadow metadata. No-op unless
    DECISION_INTELLIGENCE_ENABLED. Never raises. Never changes root_cause /
    confidence."""
    if not _flag("DECISION_INTELLIGENCE_ENABLED"):
        return
    try:
        attribution = evidence_attribution(result)
        arbitration = decision_arbitration(result)
        stability = decision_stability(result, attribution)
        sensitivity = sensitivity_analysis(result, attribution)
        residual = residual_uncertainty(result)
        explanation = explainability(
            result, attribution, arbitration, stability, residual)
        quality = decision_quality(
            result, attribution, arbitration, stability, residual)

        result["_decision_intelligence"] = {
            "evidence_attribution": attribution,
            "decision_arbitration": arbitration,
            "decision_stability": stability,
            "sensitivity_analysis": sensitivity,
            "residual_uncertainty": residual,
            "explainability": explanation,
            "decision_quality": quality,
            "decision_summary": {
                "winner": arbitration["winner"],
                "stable": stability["stable"],
                "decisive_evidence": attribution["decisive_evidence"],
                "remaining_uncertainty": residual["remaining_uncertainty"],
                "overall_decision_quality": quality["overall_decision_quality"],
            },
        }
    except Exception as exc:
        logger.warning(
            "decision_intelligence.failed error_type=%s error=%s",
            type(exc).__name__, exc)


__all__ = [
    "decision_arbitration", "decision_quality", "decision_stability",
    "evidence_attribution", "explainability", "residual_uncertainty",
    "run_decision_intelligence", "sensitivity_analysis",
]
