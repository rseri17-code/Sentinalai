"""Investigation Effectiveness Program (IEP) — produce-only value measurement.

Answers ONE question per investigation, from evidence not opinion:
"what measurable operational improvement would Investigation Intelligence
(Tranches 1-5) have created?" — NOT "what did it produce?".

Hard truth this module is built around (see INDEPENDENT_ARCHITECTURE_REVIEW):
the tranches are SHADOW-ONLY and never change the authoritative root_cause /
confidence. So the only measurable "counterfactual improvement" lives where
the shadow's INDEPENDENT re-derivation DIVERGES from the authoritative answer
— and even then, "better vs worse" needs ground truth. Where ground truth is
absent, this module reports NOT_MEASURED; it never infers a benefit.

STRICT SCOPE: offline, deterministic, replayable, produce-only. Composes
existing shadow outputs (no recomputation). Changes no runtime path, no
authority, no Wave 3. Imported by tests + offline reporting only.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.scientific_validation import (
    NOT_MEASURED,
    bootstrap_ci,
    rca_correct,
)

IEP_SCHEMA_VERSION = 1

BENEFIT_LEVELS = ("NO_BENEFIT", "MINOR", "MODERATE", "MAJOR", "UNKNOWN")
TRANCHES = ("hypothesis", "adaptive", "causal", "validation", "decision")


def _round(x: float) -> float:
    return round(float(x), 4)


def _tokens(s: str) -> set[str]:
    out: set[str] = set()
    for tok in str(s or "").lower().split():
        t = tok.strip(".,;:()[]!?\"'`<>-")
        if len(t) >= 3:
            out.add(t)
    return out


def _overlap(a: str, b: str) -> bool:
    return bool(_tokens(a) & _tokens(b))


# ---------------------------------------------------------------------------
# Phase 1 — Counterfactual comparison (authoritative vs shadow)
# ---------------------------------------------------------------------------

def counterfactual(result: Mapping[str, Any],
                   ground_truth: Mapping[str, Any] | None = None,
                   ) -> dict[str, Any]:
    """Compare the authoritative investigation to the shadow re-derivation.
    Composition only; direction (improved/worse) requires ground truth."""
    auth_rc = str(result.get("root_cause", ""))
    val = result.get("_investigation_validation") or {}
    di = result.get("_decision_intelligence") or {}
    causal = result.get("_causal_investigation") or {}

    # Shadow independent winner (T5 arbitration, else T4 concordance).
    shadow_rc = ""
    if isinstance(di, dict):
        shadow_rc = str((di.get("decision_arbitration") or {}).get("winner", ""))
    if not shadow_rc and isinstance(val, dict):
        shadow_rc = str((val.get("expert_concordance") or {}).get(
            "independent_winner", ""))

    rca_relation = "no_shadow_winner"
    if shadow_rc:
        rca_relation = "identical" if _overlap(shadow_rc, auth_rc) \
            else "divergent"

    # Localization depth: did T3 localize deeper than the symptom service?
    loc = causal.get("localization", {}) if isinstance(causal, dict) else {}
    root_svc = str(loc.get("root_cause_service", ""))
    sym_svc = str(loc.get("symptom_service", ""))
    localization_gain = bool(root_svc and sym_svc and root_svc != sym_svc)

    # Would validation have gated a weak conclusion?
    status = ""
    if isinstance(val, dict):
        status = str((val.get("root_cause_verification") or {}).get(
            "verification_status", ""))
    validation_gate = status in ("suggests", "insufficient", "contradicts")

    # Ground-truth direction (only when labeled).
    auth_correct = rca_correct(auth_rc, ground_truth or {})
    shadow_correct = rca_correct(shadow_rc, ground_truth or {}) \
        if shadow_rc else None
    direction = NOT_MEASURED
    if auth_correct is not None and shadow_correct is not None:
        if shadow_correct and not auth_correct:
            direction = "shadow_improved_rca"
        elif auth_correct and not shadow_correct:
            direction = "shadow_worse_rca"
        else:
            direction = "same_rca_outcome"

    return {
        "authoritative_root_cause": auth_rc,
        "shadow_independent_winner": shadow_rc,
        "rca_relation": rca_relation,
        "localization_gain": localization_gain,
        "validation_would_gate": validation_gate,
        "verification_status": status or NOT_MEASURED,
        "ground_truth_direction": direction,
    }


# ---------------------------------------------------------------------------
# Phase 2 + 5 — Per-tranche benefit attribution & decision attribution
# ---------------------------------------------------------------------------

def tranche_attribution(result: Mapping[str, Any]) -> dict[str, Any]:
    """Per-tranche benefit level from measurable, composed signals only.
    'differentiating' marks whether the tranche produced a signal that could
    change the decision (vs decorative)."""
    graph = result.get("_hypothesis_graph") or {}
    narr = result.get("_elimination_narrative") or {}
    adaptive = result.get("_adaptive_investigation") or {}
    causal = result.get("_causal_investigation") or {}
    val = result.get("_investigation_validation") or {}
    di = result.get("_decision_intelligence") or {}

    out: dict[str, Any] = {}

    # T1 hypothesis: a genuine differential + elimination.
    hyps = graph.get("hypotheses", []) if isinstance(graph, dict) else []
    ruled_out = narr.get("ruled_out", []) if isinstance(narr, dict) else []
    survived = bool(narr.get("survived_disconfirmation")) \
        if isinstance(narr, dict) else False
    if not hyps:
        out["hypothesis"] = _attr("NO_BENEFIT", False, "no hypothesis graph")
    elif len(hyps) >= 2 and ruled_out:
        out["hypothesis"] = _attr(
            "MAJOR" if survived else "MODERATE", True,
            f"{len(hyps)} hypotheses, {len(ruled_out)} eliminated"
            + ("; winner survived disconfirmation" if survived else ""))
    elif len(hyps) >= 2:
        out["hypothesis"] = _attr("MODERATE", True, "differential considered")
    else:
        out["hypothesis"] = _attr("MINOR", False, "single hypothesis")

    # T2 adaptive: targeted next-best-evidence reduces effort.
    nbe = adaptive.get("next_best_evidence", []) if isinstance(adaptive, dict) \
        else []
    if nbe:
        out["adaptive"] = _attr("MODERATE", True,
                                "recommended next-best evidence (effort focus)")
    elif isinstance(adaptive, dict) and adaptive.get("uncertainty_map"):
        out["adaptive"] = _attr("MINOR", False, "uncertainty map only")
    else:
        out["adaptive"] = _attr("NO_BENEFIT", False, "no adaptive output")

    # T3 causal: localization deeper than symptom = real gain.
    loc = causal.get("localization", {}) if isinstance(causal, dict) else {}
    root_svc = str(loc.get("root_cause_service", ""))
    sym_svc = str(loc.get("symptom_service", ""))
    elim = causal.get("eliminated_chains", []) if isinstance(causal, dict) \
        else []
    if root_svc and sym_svc and root_svc != sym_svc:
        out["causal"] = _attr("MAJOR" if elim else "MODERATE", True,
                              f"localized to {root_svc} vs symptom {sym_svc}")
    elif root_svc:
        out["causal"] = _attr("MINOR", False, "symptom-level localization only")
    else:
        out["causal"] = _attr("NO_BENEFIT", False, "no localization")

    # T4 validation: prevents weak conclusions.
    status = str((val.get("root_cause_verification") or {}).get(
        "verification_status", "")) if isinstance(val, dict) else ""
    contra = (val.get("evidence_validation") or {}).get(
        "contradicting_evidence", []) if isinstance(val, dict) else []
    if status in ("insufficient", "contradicts"):
        out["validation"] = _attr("MAJOR", True,
                                  f"would gate a weak conclusion ({status})")
    elif status == "suggests" or contra:
        out["validation"] = _attr("MODERATE", True,
                                  "flagged qualified/contradicted conclusion")
    elif status in ("proves", "supports"):
        out["validation"] = _attr("MINOR", False,
                                  f"confirmed conclusion ({status})")
    else:
        out["validation"] = _attr("NO_BENEFIT", False, "no validation output")

    # T5 decision: identified decisive evidence / instability.
    decisive = (di.get("evidence_attribution") or {}).get(
        "decisive_evidence", []) if isinstance(di, dict) else []
    stable = (di.get("decision_stability") or {}).get("stable") \
        if isinstance(di, dict) else None
    if decisive:
        out["decision"] = _attr("MAJOR", True,
                                f"identified decisive evidence: {sorted(decisive)}")
    elif stable is False:
        out["decision"] = _attr("MODERATE", True, "flagged unstable decision")
    elif isinstance(di, dict) and di.get("decision_quality"):
        out["decision"] = _attr("MINOR", False, "decision quality score only")
    else:
        out["decision"] = _attr("NO_BENEFIT", False, "no decision output")

    return out


def _attr(level: str, differentiating: bool, evidence: str) -> dict[str, Any]:
    return {"benefit": level, "differentiating": differentiating,
            "evidence": evidence}


# ---------------------------------------------------------------------------
# Phase 3 — Investigation Benefit Score (per corpus, with n / CI)
# ---------------------------------------------------------------------------

_BENEFIT_WEIGHT = {"NO_BENEFIT": 0.0, "MINOR": 0.25, "MODERATE": 0.6,
                   "MAJOR": 1.0, "UNKNOWN": 0.0}


def benefit_score(results: Iterable[Mapping[str, Any]],
                  labels: Mapping[str, Mapping[str, Any]] | None = None,
                  *, seed: int = 1) -> dict[str, Any]:
    """Deterministic corpus-level benefit score with sample size + CI, plus
    the ground-truth direction distribution (labeled only)."""
    labels = labels or {}
    res = list(results)
    per_tranche_scores: dict[str, list[float]] = {t: [] for t in TRANCHES}
    directions: dict[str, int] = {}
    localization_gains = 0
    validation_gates = 0
    divergent = 0
    n = 0
    for r in res:
        n += 1
        attr = tranche_attribution(r)
        for t in TRANCHES:
            per_tranche_scores[t].append(
                _BENEFIT_WEIGHT.get(attr[t]["benefit"], 0.0))
        gt = labels.get(str(r.get("incident_id", "")), {})
        cf = counterfactual(r, gt)
        directions[cf["ground_truth_direction"]] = directions.get(
            cf["ground_truth_direction"], 0) + 1
        localization_gains += 1 if cf["localization_gain"] else 0
        validation_gates += 1 if cf["validation_would_gate"] else 0
        divergent += 1 if cf["rca_relation"] == "divergent" else 0

    tranche_benefit = {
        t: _metric(per_tranche_scores[t], seed=seed,
                   limitations="composed benefit weight; not an RCA-accuracy "
                   "measurement")
        for t in TRANCHES
    }
    return {
        "schema_version": IEP_SCHEMA_VERSION,
        "n": n,
        "labeled": _count_labeled(labels, n),
        "tranche_benefit": tranche_benefit,
        "localization_gain_rate": _round(localization_gains / n) if n
        else NOT_MEASURED,
        "validation_gate_rate": _round(validation_gates / n) if n
        else NOT_MEASURED,
        "shadow_divergence_rate": _round(divergent / n) if n else NOT_MEASURED,
        "ground_truth_directions": dict(sorted(directions.items())),
        "limitations": ("benefit scores compose shadow signals; they measure "
                        "SIGNAL PRESENCE/STRENGTH, not proven RCA improvement, "
                        "which requires labeled ground truth"),
    }


def _count_labeled(labels: Mapping[str, Any], n: int) -> int:
    return sum(1 for v in labels.values()
              if isinstance(v, dict) and (v.get("root_cause")
                                          or v.get("root_cause_keywords")))


def _metric(values: list[float], *, seed: int = 1,
            limitations: str = "") -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"value": NOT_MEASURED, "n": 0, "limitations": limitations}
    ci = bootstrap_ci(values, seed=seed)
    return {"value": _round(sum(values) / n), "n": n,
            "ci95": [ci.get("lo"), ci.get("hi")],
            "underpowered": ci.get("underpowered", n < 30),
            "limitations": limitations}


# ---------------------------------------------------------------------------
# Phase 5 — Decision attribution (which tranche differentiates vs decorative)
# ---------------------------------------------------------------------------

def decision_attribution(results: Iterable[Mapping[str, Any]],
                         ) -> dict[str, Any]:
    """Across the corpus, how often each tranche produced a differentiating
    signal (could change the decision) vs was decorative (never did)."""
    counts = {t: {"differentiating": 0, "present": 0} for t in TRANCHES}
    n = 0
    for r in results:
        n += 1
        attr = tranche_attribution(r)
        for t in TRANCHES:
            if attr[t]["benefit"] != "NO_BENEFIT":
                counts[t]["present"] += 1
            if attr[t]["differentiating"]:
                counts[t]["differentiating"] += 1
    verdicts = {}
    for t in TRANCHES:
        d = counts[t]["differentiating"]
        rate = _round(d / n) if n else NOT_MEASURED
        if n == 0:
            verdicts[t] = {"rate": NOT_MEASURED, "verdict": NOT_MEASURED}
        elif d == 0:
            verdicts[t] = {"rate": rate, "verdict": "DECORATIVE_ON_CORPUS"}
        elif rate >= 0.5:
            verdicts[t] = {"rate": rate, "verdict": "CONSISTENTLY_MATTERS"}
        else:
            verdicts[t] = {"rate": rate, "verdict": "SOMETIMES_MATTERS"}
    return {"schema_version": IEP_SCHEMA_VERSION, "n": n,
            "per_tranche": {t: {**counts[t], **verdicts[t]}
                            for t in TRANCHES}}


# ---------------------------------------------------------------------------
# Phase 6 — Scientific effectiveness verdict
# ---------------------------------------------------------------------------

def scientific_verdict(benefit: Mapping[str, Any],
                       *, min_labeled: int = 30) -> dict[str, Any]:
    """YES / NO / INCONCLUSIVE that Investigation Intelligence produces a
    statistically-supported operational benefit. Fail-honest: with no labeled
    outcomes the RCA-benefit question is NOT_MEASURED / INCONCLUSIVE."""
    dirs = benefit.get("ground_truth_directions", {}) or {}
    improved = dirs.get("shadow_improved_rca", 0)
    worse = dirs.get("shadow_worse_rca", 0)
    labeled = improved + worse + dirs.get("same_rca_outcome", 0)

    if labeled < min_labeled:
        verdict = "INCONCLUSIVE"
        reason = (f"only {labeled} labeled outcomes (< {min_labeled} power "
                  f"floor); RCA-benefit direction is NOT_MEASURED")
    elif improved > worse and (improved - worse) >= 0.05 * labeled:
        verdict = "YES"
        reason = f"shadow improved RCA on {improved} vs worsened {worse}"
    elif worse > improved:
        verdict = "NO"
        reason = f"shadow worsened RCA on {worse} vs improved {improved}"
    else:
        verdict = "INCONCLUSIVE"
        reason = "no significant RCA-benefit difference"

    return {
        "schema_version": IEP_SCHEMA_VERSION,
        "rca_benefit_verdict": verdict,
        "reason": reason,
        "labeled_outcomes": labeled,
        "improved": improved,
        "worse": worse,
        "required_additional_evidence": (
            f"{max(0, min_labeled - labeled)} more labeled outcomes; a "
            "held-out, leakage-free corpus; per-class n>=20"),
        "limitations": ("shadow stack is non-authoritative; a positive verdict "
                        "bounds POTENTIAL benefit if promoted, not realized "
                        "benefit today"),
    }


# ---------------------------------------------------------------------------
# Phase 7 — Retirement analysis
# ---------------------------------------------------------------------------

def retirement_analysis(decision_attr: Mapping[str, Any]) -> dict[str, Any]:
    """Retain / Simplify / Retire / Merge per tranche — never preserve
    complexity without measured benefit. Honest under low N: a DECORATIVE
    verdict on a tiny corpus recommends 'measure more', not 'retire'."""
    n = decision_attr.get("n", 0)
    per = decision_attr.get("per_tranche", {})
    recs = {}
    for t in TRANCHES:
        v = per.get(t, {}).get("verdict")
        if n < 30:
            recs[t] = {"recommendation": "RETAIN_PENDING_EVIDENCE",
                       "reason": f"corpus n={n} too small to judge retirement"}
        elif v == "DECORATIVE_ON_CORPUS":
            recs[t] = {"recommendation": "RETIRE_OR_SIMPLIFY",
                       "reason": "never differentiated across a powered corpus"}
        elif v == "CONSISTENTLY_MATTERS":
            recs[t] = {"recommendation": "RETAIN",
                       "reason": "differentiates in a majority of investigations"}
        else:
            recs[t] = {"recommendation": "RETAIN",
                       "reason": "sometimes differentiates"}
    return {"schema_version": IEP_SCHEMA_VERSION, "n": n,
            "recommendations": recs}


# ---------------------------------------------------------------------------
# Phase 8 — Promotion readiness
# ---------------------------------------------------------------------------

def promotion_readiness(verdict: Mapping[str, Any],
                        decision_attr: Mapping[str, Any]) -> dict[str, Any]:
    """Which tranche, if any, has earned LIMITED runtime authority under the
    existing gate philosophy. Fail-closed: no promotion without a YES verdict
    on a powered, labeled corpus."""
    rca = verdict.get("rca_benefit_verdict")
    labeled = verdict.get("labeled_outcomes", 0)
    if rca != "YES" or labeled < 30:
        return {
            "schema_version": IEP_SCHEMA_VERSION,
            "promote": "NO_TRANCHES",
            "reason": (f"RCA-benefit verdict is {rca} on {labeled} labeled "
                       "outcomes; the gate philosophy forbids granting runtime "
                       "authority without measured, powered benefit evidence"),
            "risk": "granting authority now would ship unproven behaviour",
            "measurement_plan": "run the shadow pilot to a powered labeled corpus",
            "rollback": "n/a — nothing promoted",
        }
    # If (hypothetically) evidence supported it, promotion order follows the
    # tranches that consistently matter AND are lowest-risk to consume.
    per = decision_attr.get("per_tranche", {})
    ordered = [t for t in ("validation", "hypothesis", "causal", "decision",
                           "adaptive")
               if per.get(t, {}).get("verdict") == "CONSISTENTLY_MATTERS"]
    return {
        "schema_version": IEP_SCHEMA_VERSION,
        "promote": ordered[:1] or "NO_TRANCHES",
        "reason": "first candidate is the lowest-risk consistently-mattering "
                  "tranche (validation gates weak conclusions without changing "
                  "the winner)",
        "risk": "read-only advisory gating first; never auto-changes root_cause",
        "measurement_plan": "A/B under the shadow pilot; compare gated vs "
                            "ungated RCA accuracy on held-out incidents",
        "rollback": "flag OFF restores byte-identical behaviour",
    }


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------

def effectiveness_report(results: Iterable[Mapping[str, Any]],
                         labels: Mapping[str, Mapping[str, Any]] | None = None,
                         ) -> dict[str, Any]:
    res = list(results)
    ben = benefit_score(res, labels)
    da = decision_attribution(res)
    verdict = scientific_verdict(ben)
    return {
        "schema_version": IEP_SCHEMA_VERSION,
        "n": len(res),
        "benefit_score": ben,
        "decision_attribution": da,
        "scientific_verdict": verdict,
        "retirement": retirement_analysis(da),
        "promotion": promotion_readiness(verdict, da),
    }


__all__ = [
    "IEP_SCHEMA_VERSION", "BENEFIT_LEVELS", "TRANCHES",
    "counterfactual", "tranche_attribution", "benefit_score",
    "decision_attribution", "scientific_verdict", "retirement_analysis",
    "promotion_readiness", "effectiveness_report",
]
