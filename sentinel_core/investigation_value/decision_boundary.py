"""Decision Boundary Analysis — locate where the shadow engine has leverage.

The Investigation Effectiveness Program proved the shadow stack AGREES with
the authoritative answer ~100% of the time. This module diagnoses WHY, and —
more usefully — measures, per decision boundary, how often a shadow signal
WOULD change or ADD to the authoritative decision if it were promoted. It
turns "the shadow never diverges" into a prioritized, evidence-backed map of
where promotion could actually move outcomes.

Produce-only, offline, deterministic, replayable. Composes existing outputs;
recomputes nothing; changes no runtime path, no authority, no Wave 3.

The authoritative decision boundaries (from the Phase-0 reality audit of
supervisor/agent.py) and the shadow signal sitting at each:

  hypothesis_ranking  agent.py:2343 `sort(-base_score); winner=[0]`   T5/T4 winner
  confidence          agent.py:2348 `confidence = winner.base_score`   T4 evidence_confidence
  localization        NONE (baseline emits no root_cause_service)      T3 localization  (NET-NEW)
  validation_gating   evidence_gates G2/G3/G5, own thresholds          T4 verification_status
  decision_arbitration NONE                                            T5 decisive_evidence (NET-NEW)

Key structural finding this module encodes: the shadow re-scores the
baseline's OWN ranked candidate set (it never generates independent
hypotheses), so on the *corrective* boundaries it can only diverge when its
re-ranking overturns base_score. On the *net-new* boundaries (localization,
decisive-evidence) the baseline emits nothing, so the shadow adds capability
rather than correcting it — a different, often safer, kind of leverage.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.scientific_validation import (
    NOT_MEASURED,
    rca_correct,
)

DECISION_BOUNDARY_SCHEMA_VERSION = 1

# type: "corrective" (baseline has a value the shadow could change) vs
# "net_new" (baseline emits nothing; the shadow adds a capability).
# base_risk reflects blast radius if promoted to authority.
BOUNDARIES = (
    {"key": "hypothesis_ranking", "signal": "T1/T5", "type": "corrective",
     "base_risk": "medium",
     "note": "shadow re-ranks the SAME base_score candidates; changes the RCA"},
    {"key": "confidence", "signal": "T4", "type": "corrective",
     "base_risk": "medium",
     "note": "evidence_confidence vs calibrated confidence; changes a number"},
    {"key": "localization", "signal": "T3", "type": "net_new",
     "base_risk": "medium",
     "note": "baseline emits no service localization; T3 adds one"},
    {"key": "validation_gating", "signal": "T4", "type": "additive_gate",
     "base_risk": "very_low",
     "note": "gates a weak conclusion; never changes the winner"},
    {"key": "decision_arbitration", "signal": "T5", "type": "net_new",
     "base_risk": "medium",
     "note": "identifies decisive evidence; no authoritative counterpart"},
)

_CONF_DELTA_THRESHOLD = 15         # |confidence - evidence_confidence| to flag
_GATE_STATUSES = ("insufficient", "contradicts", "suggests")
_ALREADY_GATED = ("INSUFFICIENT", "LOW CONFIDENCE", "BLOCKED")


def _round(x: float) -> float:
    return round(float(x), 4)


def _tokens(s: str) -> set[str]:
    out: set[str] = set()
    for tok in str(s or "").lower().split():
        t = tok.strip(".,;:()[]!?\"'`<>-")
        if len(t) >= 3:
            out.add(t)
    return out


# ---------------------------------------------------------------------------
# Per-investigation boundary probe
# ---------------------------------------------------------------------------

def boundary_probe(result: Mapping[str, Any]) -> dict[str, Any]:
    """For one investigation, at each boundary: is the shadow signal present,
    and would it change (corrective) or add to (net-new) the authoritative
    decision?"""
    auth_rc = str(result.get("root_cause", ""))
    conf = int(result.get("confidence", 0) or 0)
    val = result.get("_investigation_validation") or {}
    di = result.get("_decision_intelligence") or {}
    causal = result.get("_causal_investigation") or {}

    # hypothesis_ranking: shadow winner vs authoritative RCA
    shadow_winner = ""
    if isinstance(di, dict):
        shadow_winner = str((di.get("decision_arbitration") or {}).get(
            "winner", ""))
    if not shadow_winner and isinstance(val, dict):
        shadow_winner = str((val.get("expert_concordance") or {}).get(
            "independent_winner", ""))
    ranking_present = bool(shadow_winner)
    ranking_change = bool(shadow_winner and not (
        _tokens(shadow_winner) & _tokens(auth_rc)))

    # confidence: evidence_confidence vs calibrated confidence
    ev_conf = None
    if isinstance(val, dict):
        ev_conf = (val.get("confidence_reconstruction") or {}).get(
            "evidence_confidence")
    conf_present = isinstance(ev_conf, (int, float))
    conf_change = conf_present and abs(int(ev_conf) - conf) >= _CONF_DELTA_THRESHOLD

    # localization: net-new (baseline has none)
    loc = causal.get("localization", {}) if isinstance(causal, dict) else {}
    loc_service = str(loc.get("root_cause_service", ""))
    loc_present = bool(loc_service)
    loc_adds = loc_present            # baseline emits nothing → any value adds

    # validation_gating: would the shadow add a gate the baseline didn't apply?
    status = ""
    if isinstance(val, dict):
        status = str((val.get("root_cause_verification") or {}).get(
            "verification_status", ""))
    already_gated = any(auth_rc.upper().startswith(p) for p in _ALREADY_GATED)
    gate_present = bool(status)
    gate_change = (status in _GATE_STATUSES) and not already_gated

    # decision_arbitration: net-new decisive evidence
    decisive = (di.get("evidence_attribution") or {}).get(
        "decisive_evidence", []) if isinstance(di, dict) else []
    arb_present = bool(di)
    arb_adds = bool(decisive)

    return {
        "hypothesis_ranking": {"present": ranking_present,
                               "would_change": ranking_change},
        "confidence": {"present": conf_present, "would_change": conf_change},
        "localization": {"present": loc_present, "adds": loc_adds},
        "validation_gating": {"present": gate_present,
                              "would_change": gate_change},
        "decision_arbitration": {"present": arb_present, "adds": arb_adds},
    }


# ---------------------------------------------------------------------------
# Corpus-level boundary analysis + prioritized promotion table
# ---------------------------------------------------------------------------

_BENEFIT_FROM_RATE = ((0.30, "High"), (0.10, "Medium"), (0.0, "Low"))


def _benefit(rate: float, boundary_type: str) -> str:
    if boundary_type == "net_new" and rate > 0:
        return "High"            # capability the baseline lacks entirely
    for thresh, label in _BENEFIT_FROM_RATE:
        if rate > thresh:
            return label
    return "None"


def _recommendation(b: Mapping[str, Any], rate: float, labeled: int,
                    ) -> str:
    """Evidence-gated recommendation. Promotion of authority always requires a
    powered labeled corpus; below that, the recommendation is shadow-first."""
    risk = b["base_risk"]
    if labeled < 30:
        # No powered evidence yet — recommend the SEQUENCE, not promotion.
        if b["type"] == "additive_gate":
            return "SAFE_FIRST_CANDIDATE — read-only gating, lowest risk; A/B once labeled"
        if b["type"] == "net_new":
            return "SHADOW_FIRST — additive capability; measure usefulness in pilot"
        if rate > 0:
            return "MORE_EVIDENCE — divergences observed; label them before A/B"
        return "MORE_EVIDENCE — no divergence yet on this corpus"
    # Powered corpus:
    if b["type"] == "additive_gate" and rate > 0:
        return "CANDIDATE_FOR_CONTROLLED_AUTHORITY (very low risk)"
    if rate >= 0.30 and risk in ("very_low", "low"):
        return "CANDIDATE_FOR_CONTROLLED_AUTHORITY"
    if rate >= 0.10:
        return "A/B_BEHIND_FLAG"
    return "RETAIN_SHADOW — insufficient leverage to justify promotion"


def boundary_analysis(results: Iterable[Mapping[str, Any]],
                      labels: Mapping[str, Mapping[str, Any]] | None = None,
                      ) -> dict[str, Any]:
    """Aggregate boundary leverage across the corpus and rank promotion
    candidates by (potential benefit, low risk, evidence)."""
    labels = labels or {}
    res = list(results)
    n = len(res)
    labeled = sum(1 for r in res
                  if rca_correct(str(r.get("root_cause", "")),
                                 labels.get(str(r.get("incident_id", "")), {}))
                  is not None)

    counts = {b["key"]: {"present": 0, "leverage": 0} for b in BOUNDARIES}
    for r in res:
        probe = boundary_probe(r)
        for b in BOUNDARIES:
            p = probe[b["key"]]
            if p.get("present"):
                counts[b["key"]]["present"] += 1
            if p.get("would_change") or p.get("adds"):
                counts[b["key"]]["leverage"] += 1

    rows = []
    for b in BOUNDARIES:
        lev = counts[b["key"]]["leverage"]
        rate = _round(lev / n) if n else 0.0
        benefit = _benefit(rate, b["type"]) if n else NOT_MEASURED
        rows.append({
            "decision_boundary": b["key"],
            "shadow_signal": b["signal"],
            "boundary_type": b["type"],
            "authoritative_today": False,      # every boundary is shadow-only
            "present_rate": _round(counts[b["key"]]["present"] / n) if n
            else NOT_MEASURED,
            "leverage_rate": rate if n else NOT_MEASURED,
            "potential_benefit": benefit,
            "risk": b["base_risk"],
            "recommendation": _recommendation(b, rate, labeled),
            "note": b["note"],
        })

    # Prioritize: benefit desc, risk asc, leverage desc.
    benefit_rank = {"High": 3, "Medium": 2, "Low": 1, "None": 0,
                    NOT_MEASURED: 0}
    risk_rank = {"very_low": 0, "low": 1, "medium": 2, "high": 3}
    ordered = sorted(
        rows,
        key=lambda x: (-benefit_rank.get(x["potential_benefit"], 0),
                       risk_rank.get(x["risk"], 9),
                       -(x["leverage_rate"] if isinstance(x["leverage_rate"],
                                                          float) else 0),
                       x["decision_boundary"]))

    # Two distinct questions, deliberately separated:
    #  - highest_leverage: where the shadow could move outcomes MOST (benefit-first)
    #  - safe_first_promotion: what to promote FIRST at LOWEST risk (risk-first
    #    among additive boundaries that never change the winner)
    row_by_key = {r["decision_boundary"]: r for r in rows}
    safe_candidates = sorted(
        (b for b in BOUNDARIES if b["type"] in ("additive_gate", "net_new")),
        key=lambda b: (risk_rank.get(b["base_risk"], 9), b["key"]))
    safe_first = safe_candidates[0]["key"] if safe_candidates and n else \
        NOT_MEASURED

    return {
        "schema_version": DECISION_BOUNDARY_SCHEMA_VERSION,
        "n": n,
        "labeled": labeled,
        "boundaries": rows,
        "priority_order": [r["decision_boundary"] for r in ordered],
        "highest_leverage": ordered[0]["decision_boundary"]
        if ordered and n else NOT_MEASURED,
        "safe_first_promotion": safe_first,
        "why_identical": _why_identical(counts, n),
        "limitations": ("leverage measures where a shadow signal WOULD change/"
                        "add to the authoritative decision; whether that change "
                        "is an IMPROVEMENT still requires labeled outcomes"),
    }


def _why_identical(counts: Mapping[str, Any], n: int) -> list[str]:
    """The evidence-backed explanations for shadow≡authoritative convergence."""
    reasons = [
        "structural: the shadow re-scores the baseline's OWN ranked candidate "
        "set (no independent hypothesis generation), so it rarely overturns the "
        "base_score argmax on the ranking boundary",
        "contractual: every decision boundary is non-authoritative — the shadow "
        "signals are wired to nothing, so they cannot influence the outcome",
    ]
    if n and n < 30:
        reasons.append(
            f"evidential: corpus n={n} is small/homogeneous and may not exercise "
            "cases where re-ranking would flip the winner")
    ranking_lev = counts.get("hypothesis_ranking", {}).get("leverage", 0)
    if n and ranking_lev == 0:
        reasons.append(
            "observed: 0 ranking divergences on this corpus — the baseline's "
            "top candidate already matches the shadow's evidence-ranked winner")
    return reasons


__all__ = [
    "DECISION_BOUNDARY_SCHEMA_VERSION", "BOUNDARIES",
    "boundary_probe", "boundary_analysis",
]
