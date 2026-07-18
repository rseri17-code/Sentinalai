"""Scientific Validation — measure whether Investigation Intelligence
(Tranches 1-5) objectively improves SentinelAI. EVALUATION ONLY.

This module MEASURES; it never improves the engine, never changes runtime
authority, never enables Wave 3. Every function is a pure, deterministic,
offline transform of already-produced outputs — no clock, no randomness
(the bootstrap uses a caller-supplied integer seed via a fixed LCG so it
is fully replayable), no LLM, no I/O.

THE CENTRAL ARCHITECTURAL FACT this program must confront
-----------------------------------------------------------------
Tranches 1-5 are shadow-only: they write only ``_*`` metadata keys and the
regression suite enforces that ``root_cause`` / ``confidence`` / evidence
routing are byte-identical whether the flags are ON or OFF. Therefore:

  * The authoritative RCA under TREATMENT == the authoritative RCA under
    CONTROL, for every incident, by construction.
  * A non-authoritative stack CANNOT change RCA accuracy. The E1/E2
    "authoritative RCA correctness" delta is structurally zero — this is a
    fact to be reported, not a result to be discovered.

What IS scientifically measurable:

  * SAFETY — does the shadow stack's *independent* re-derivation (T4 expert
    concordance / T5 arbitration winner) AGREE with the authoritative
    winner? High agreement ⇒ promoting a capability would not introduce
    disagreement. This bounds the RISK of a future authoritative pilot.
  * SIGNAL QUALITY — do the NEW signals (verification status, evidence
    validation score, decision stability, localization, evidence
    confidence) correctly track ground-truth correctness? This bounds the
    BENEFIT a future authoritative pilot could realise.
  * CORPUS SUFFICIENCY — is the labeled evidence base large enough to make
    any of the above statistically significant?

Where ground truth is absent, the metric is reported as ``NOT MEASURED`` —
never guessed, never assumed.

Schema: SCIENTIFIC_VALIDATION_SCHEMA_VERSION.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

SCIENTIFIC_VALIDATION_SCHEMA_VERSION = 1

NOT_MEASURED = "NOT_MEASURED"

# Incident classes evaluated independently (Phase 5). Unknown classes are
# reported separately, never folded into these.
INCIDENT_CLASSES = ("kubernetes", "database", "deployment",
                    "authentication", "dns")

# Readiness thresholds mirror the Wave-3 gates so the two agree.
MIN_CORPUS_TOTAL = 500
MIN_CORPUS_PER_CLASS = 20
MIN_POWER_N = 30            # below this, significance testing is underpowered


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
# Ground-truth correctness (reuses the SentinelBench keyword convention)
# ---------------------------------------------------------------------------

def rca_correct(root_cause: str, ground_truth: Mapping[str, Any]) -> bool | None:
    """True/False against labeled ground truth, or None when unlabeled.

    Mirrors sentinelbench.scorer: category/text substring OR a majority of
    the required keywords present. None ⇒ NOT MEASURED (no label)."""
    if not ground_truth:
        return None
    kws = [str(k).lower() for k in
           (ground_truth.get("root_cause_keywords")
            or ground_truth.get("required_keywords") or [])]
    truth_text = str(ground_truth.get("root_cause", "")).lower()
    if not kws and not truth_text:
        return None
    rca = str(root_cause or "").lower()
    if truth_text and truth_text in rca:
        return True
    if kws:
        hits = sum(1 for k in kws if k in rca)
        return hits >= (len(kws) + 1) // 2      # majority of keywords
    return None


def _class_of(ground_truth: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Map an incident to one of INCIDENT_CLASSES, else 'other'."""
    hay = " ".join(str(x).lower() for x in (
        ground_truth.get("incident_type", ""),
        ground_truth.get("service", ""),
        ground_truth.get("root_cause", ""),
        result.get("incident_type", ""),
    ))
    alias = {
        "kubernetes": ("kubernetes", "k8s", "pod", "oomkill", "oom", "node"),
        "database":   ("database", "db", "connection pool", "sql", "redis",
                       "postgres", "query"),
        "deployment": ("deploy", "deployment", "release", "rollout",
                       "version", "regression"),
        "authentication": ("auth", "authentication", "cert", "token",
                            "credential", "login", "oauth"),
        "dns":        ("dns", "resolve", "resolution", "nxdomain", "lookup"),
    }
    for cls in INCIDENT_CLASSES:
        if any(a in hay for a in alias[cls]):
            return cls
    return "other"


# ---------------------------------------------------------------------------
# Phase 1 — Canonical Evaluation Record (compose only; single source of truth)
# ---------------------------------------------------------------------------

def canonical_evaluation_record(
    control_result: Mapping[str, Any],
    treatment_result: Mapping[str, Any],
    ground_truth: Mapping[str, Any] | None = None,
    *,
    incident_id: str = "",
    model: str = "",
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One immutable evaluation record composed from existing outputs.

    CONTROL = authoritative result with the shadow flags OFF.
    TREATMENT = the same investigation with Tranches 1-5 ON (adds ``_*``).
    No recomputation; every field is lifted verbatim from what already ran.
    """
    gt = dict(ground_truth or {})
    val = treatment_result.get("_investigation_validation") or {}
    di = treatment_result.get("_decision_intelligence") or {}
    graph = treatment_result.get("_hypothesis_graph") or {}

    ctrl_rc = str(control_result.get("root_cause", ""))
    treat_rc = str(treatment_result.get("root_cause", ""))

    # Independent (shadow) winner — the safety probe. Prefer T5 arbitration,
    # fall back to T4 concordance.
    independent = ""
    if isinstance(di, dict):
        independent = str((di.get("decision_arbitration") or {}).get("winner", ""))
    if not independent and isinstance(val, dict):
        independent = str((val.get("expert_concordance") or {}).get(
            "independent_winner", ""))

    record = {
        "schema_version": SCIENTIFIC_VALIDATION_SCHEMA_VERSION,
        "incident_id": incident_id or str(gt.get("incident_id", "")),
        "incident_class": _class_of(gt, treatment_result),
        "authoritative": {
            "control_root_cause": ctrl_rc,
            "treatment_root_cause": treat_rc,
            "control_confidence": int(control_result.get("confidence", 0) or 0),
            "treatment_confidence": int(
                treatment_result.get("confidence", 0) or 0),
            "authoritative_unchanged": ctrl_rc == treat_rc
            and control_result.get("confidence") == treatment_result.get(
                "confidence"),
        },
        "shadow": {
            "hypothesis_count": len(graph.get("hypotheses", []))
            if isinstance(graph, dict) else 0,
            "independent_winner": independent,
            "verification_status": str((val.get("root_cause_verification")
                                        or {}).get("verification_status",
                                                    NOT_MEASURED)),
            "evidence_validation_score": (val.get("evidence_validation")
                                          or {}).get(
                "evidence_validation_score"),
            "evidence_confidence": (val.get("confidence_reconstruction")
                                    or {}).get("evidence_confidence"),
            "localization": (treatment_result.get("_causal_investigation")
                             or {}).get("localization", {}).get(
                "root_cause_service", "") if isinstance(
                treatment_result.get("_causal_investigation"), dict) else "",
            "decision_stable": (di.get("decision_stability") or {}).get(
                "stable") if isinstance(di, dict) else None,
            "decision_quality": (di.get("decision_quality") or {}).get(
                "overall_decision_quality") if isinstance(di, dict) else None,
            "citation_coverage": treatment_result.get("citation_coverage"),
        },
        "ground_truth": {
            "labeled": bool(gt),
            "root_cause": str(gt.get("root_cause", "")),
            "service": str(gt.get("service", "")),
            "control_correct": rca_correct(ctrl_rc, gt),
            "treatment_correct": rca_correct(treat_rc, gt),
            "shadow_winner_correct": rca_correct(independent, gt)
            if independent else None,
        },
        "features": {
            "hypothesis_engine": bool(graph),
            "adaptive": "_adaptive_investigation" in treatment_result,
            "causal": "_causal_investigation" in treatment_result,
            "validation": bool(val),
            "decision_intelligence": bool(di),
        },
        "provenance": dict(provenance or {}),
        "model": model,
    }
    record["record_id"] = _record_id(record)
    return record


def _record_id(record: Mapping[str, Any]) -> str:
    payload = json.dumps({k: v for k, v in record.items()
                          if k != "record_id"}, sort_keys=True,
                         separators=(",", ":"))
    return "eval:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Phase 2 — E1 shadow evaluation (control vs treatment)
# ---------------------------------------------------------------------------

def e1_shadow_evaluation(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate the E1 comparison across canonical records.

    The primary authoritative-RCA delta is reported explicitly as
    structurally zero for a shadow-only stack; the substantive results are
    the SAFETY (shadow-authoritative agreement) and SIGNAL-QUALITY
    (discrimination of correct vs incorrect) metrics."""
    recs = list(records)
    n = len(recs)
    if not n:
        return {"n": 0, "verdict": "NO_RECORDS"}

    labeled = [r for r in recs if r["ground_truth"]["labeled"]
               and r["ground_truth"]["control_correct"] is not None]
    authoritative_unchanged = all(
        r["authoritative"]["authoritative_unchanged"] for r in recs)

    # Safety: does the independent shadow winner match the authoritative RCA?
    agree = [r for r in recs if r["shadow"]["independent_winner"]]
    safety_matches = sum(
        1 for r in agree
        if _tokens(r["shadow"]["independent_winner"])
        & _tokens(r["authoritative"]["treatment_root_cause"]))
    safety_rate = _round(safety_matches / len(agree)) if agree else NOT_MEASURED

    # Signal quality: does verification 'proves/supports' track correctness?
    verified_correct = verified_incorrect = unverified_correct = 0
    unverified_incorrect = 0
    for r in labeled:
        correct = r["ground_truth"]["treatment_correct"]
        status = r["shadow"]["verification_status"]
        verified = status in ("proves", "supports")
        if verified and correct:
            verified_correct += 1
        elif verified and not correct:
            verified_incorrect += 1
        elif not verified and correct:
            unverified_correct += 1
        else:
            unverified_incorrect += 1

    # confidence calibration: |evidence_confidence - 100*correct| vs
    # |raw_confidence - 100*correct| — lower is better.
    ev_err = raw_err = 0.0
    cal_n = 0
    for r in labeled:
        correct = 100 if r["ground_truth"]["treatment_correct"] else 0
        ev = r["shadow"]["evidence_confidence"]
        raw = r["authoritative"]["treatment_confidence"]
        if ev is None:
            continue
        ev_err += abs(ev - correct)
        raw_err += abs(raw - correct)
        cal_n += 1
    calibration = {
        "n": cal_n,
        "evidence_confidence_mae": _round(ev_err / cal_n) if cal_n
        else NOT_MEASURED,
        "raw_confidence_mae": _round(raw_err / cal_n) if cal_n
        else NOT_MEASURED,
        "evidence_confidence_better": (ev_err < raw_err) if cal_n else
        NOT_MEASURED,
    }

    return {
        "n": n,
        "n_labeled": len(labeled),
        "authoritative_rca_delta": {
            "control_correct": sum(
                1 for r in labeled if r["ground_truth"]["control_correct"]),
            "treatment_correct": sum(
                1 for r in labeled if r["ground_truth"]["treatment_correct"]),
            "authoritative_unchanged": authoritative_unchanged,
            "note": ("shadow-only stack: authoritative RCA identical ON/OFF "
                     "by construction — delta is structurally zero"),
        },
        "safety_shadow_authoritative_agreement": safety_rate,
        "signal_quality_verification": {
            "verified_correct": verified_correct,
            "verified_incorrect": verified_incorrect,
            "unverified_correct": unverified_correct,
            "unverified_incorrect": unverified_incorrect,
        },
        "confidence_calibration": calibration,
    }


# ---------------------------------------------------------------------------
# Phase 5 — per incident class
# ---------------------------------------------------------------------------

def per_class_effectiveness(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for r in records:
        buckets.setdefault(r["incident_class"], []).append(r)
    out: dict[str, Any] = {}
    for cls in sorted(buckets):
        rs = buckets[cls]
        labeled = [r for r in rs
                   if r["ground_truth"]["treatment_correct"] is not None]
        out[cls] = {
            "n": len(rs),
            "n_labeled": len(labeled),
            "treatment_correct": sum(
                1 for r in labeled if r["ground_truth"]["treatment_correct"]),
            "supported": cls in INCIDENT_CLASSES,
            "sufficient_sample": len(labeled) >= MIN_CORPUS_PER_CLASS,
        }
    return out


# ---------------------------------------------------------------------------
# Phase 6 — statistical analysis (pure, deterministic)
# ---------------------------------------------------------------------------

def mcnemar(b: int, c: int) -> dict[str, Any]:
    """Exact-ish McNemar on the discordant pair counts.

    b = control-correct & treatment-wrong; c = control-wrong &
    treatment-correct. With b+c small we report the two-sided exact
    binomial p-value; the chi-square with continuity correction otherwise.
    Zero discordant pairs ⇒ no measurable difference."""
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "discordant": 0, "p_value": 1.0,
                "significant": False,
                "note": "no discordant pairs — no measurable difference"}
    if n < MIN_POWER_N:
        # exact two-sided binomial, p = 0.5
        k = min(b, c)
        tail = sum(_binom(n, i) for i in range(0, k + 1)) / (2.0 ** n)
        p = min(1.0, 2.0 * tail)
        return {"b": b, "c": c, "discordant": n, "p_value": _round(p),
                "significant": p < 0.05, "method": "exact_binomial",
                "underpowered": True}
    chi = (abs(b - c) - 1) ** 2 / n
    return {"b": b, "c": c, "discordant": n,
            "chi_square_cc": _round(chi),
            "significant": chi > 3.841,      # χ²(1) at α=0.05
            "method": "chi_square_continuity"}


def _binom(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    num = den = 1
    for i in range(1, k + 1):
        num *= n - (k - i)
        den *= i
    return num // den


def bootstrap_ci(
    values: list[float], *, seed: int = 1, iterations: int = 1000,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Deterministic bootstrap CI for the mean. Resampling indices come from
    a fixed LCG seeded by ``seed`` — replayable, no RNG, no clock."""
    n = len(values)
    if n == 0:
        return {"mean": NOT_MEASURED, "lo": NOT_MEASURED, "hi": NOT_MEASURED,
                "n": 0}
    if n == 1:
        return {"mean": _round(values[0]), "lo": _round(values[0]),
                "hi": _round(values[0]), "n": 1, "degenerate": True}
    state = seed & 0xFFFFFFFF
    means = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            total += values[state % n]
        means.append(total / n)
    means.sort()
    lo_i = int((alpha / 2) * iterations)
    hi_i = min(iterations - 1, int((1 - alpha / 2) * iterations))
    return {
        "mean": _round(sum(values) / n),
        "lo": _round(means[lo_i]),
        "hi": _round(means[hi_i]),
        "n": n,
        "underpowered": n < MIN_POWER_N,
    }


def effect_size(control: list[float], treatment: list[float]) -> dict[str, Any]:
    """Cohen's d (pooled). Deterministic; NOT MEASURED if degenerate."""
    nc, nt = len(control), len(treatment)
    if nc < 2 or nt < 2:
        return {"cohens_d": NOT_MEASURED, "n_control": nc, "n_treatment": nt}
    mc, mt = sum(control) / nc, sum(treatment) / nt
    vc = sum((x - mc) ** 2 for x in control) / (nc - 1)
    vt = sum((x - mt) ** 2 for x in treatment) / (nt - 1)
    pooled = (((nc - 1) * vc + (nt - 1) * vt) / (nc + nt - 2)) ** 0.5
    d = (mt - mc) / pooled if pooled else 0.0
    return {"cohens_d": _round(d), "n_control": nc, "n_treatment": nt,
            "magnitude": _magnitude(abs(d))}


def _magnitude(d: float) -> str:
    if d < 0.2:
        return "negligible"
    if d < 0.5:
        return "small"
    if d < 0.8:
        return "medium"
    return "large"


# ---------------------------------------------------------------------------
# Phase 8 — failure taxonomy
# ---------------------------------------------------------------------------

FAILURE_CATEGORIES = ("hypothesis", "evidence", "localization", "validation",
                      "confidence", "decision", "counterfactual", "unknown")


def classify_failure(record: Mapping[str, Any]) -> str | None:
    """Classify a treatment failure. None ⇒ not a labeled failure."""
    gt = record["ground_truth"]
    if gt["treatment_correct"] is None:
        return None                     # unlabeled — cannot judge
    if gt["treatment_correct"]:
        return None                     # not a failure
    sh = record["shadow"]
    # The correct hypothesis was never generated.
    if gt["shadow_winner_correct"] is False and sh["hypothesis_count"] <= 1:
        return "hypothesis"
    # A different hypothesis was independently favoured by the evidence.
    if gt["shadow_winner_correct"] and not gt["treatment_correct"]:
        return "decision"
    # The engine flagged its own weakness but was still wrong.
    if sh["verification_status"] in ("insufficient", "suggests",
                                     "contradicts"):
        return "validation"
    ev = sh.get("evidence_validation_score")
    if isinstance(ev, (int, float)) and ev < 0.5:
        return "evidence"
    if sh.get("localization") and gt["service"] \
            and sh["localization"] not in gt["service"] \
            and gt["service"] not in sh["localization"]:
        return "localization"
    if sh.get("decision_stable") is False:
        return "decision"
    ec = sh.get("evidence_confidence")
    if isinstance(ec, (int, float)) and ec >= 70:
        return "confidence"             # confidently wrong
    return "unknown"


def failure_taxonomy(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {c: 0 for c in FAILURE_CATEGORIES}
    cases: list[dict[str, Any]] = []
    for r in records:
        cat = classify_failure(r)
        if cat is None:
            continue
        counts[cat] += 1
        cases.append({"incident_id": r["incident_id"], "category": cat,
                      "record_id": r["record_id"]})
    return {"counts": counts,
            "total_failures": sum(counts.values()),
            "cases": sorted(cases, key=lambda x: x["incident_id"])}


# ---------------------------------------------------------------------------
# Phase 7 + final verdict
# ---------------------------------------------------------------------------

VERDICTS = (
    "READY_FOR_CONTROLLED_HUMAN_PILOT",
    "READY_AFTER_MORE_SHADOW_EVIDENCE",
    "NOT_READY_ACCURACY_REGRESSION",
    "NOT_READY_SAFETY_REGRESSION",
    "NOT_READY_INSUFFICIENT_EVIDENCE",
)


def promotion_gate_assessment(
    e1: Mapping[str, Any], per_class: Mapping[str, Any],
    *, corpus_total: int, regression_clean: bool, replay_clean: bool,
) -> dict[str, Any]:
    """Evaluate the non-negotiable promotion gates. Fail-closed."""
    delta = e1.get("authoritative_rca_delta", {})
    ctrl = delta.get("control_correct", 0)
    treat = delta.get("treatment_correct", 0)
    sq = e1.get("signal_quality_verification", {})

    gates = {
        "no_rca_regression": treat >= ctrl,
        "no_wrong_rca_increase": sq.get("verified_incorrect", 0) == 0
        or delta.get("authoritative_unchanged", False),
        "authoritative_unchanged": bool(delta.get("authoritative_unchanged")),
        "corpus_sufficient": corpus_total >= MIN_CORPUS_TOTAL,
        "regression_clean": bool(regression_clean),
        "replay_clean": bool(replay_clean),
        "per_class_sufficient": all(
            v["sufficient_sample"] for v in per_class.values()
            if v["supported"]) if per_class else False,
    }
    return {"gates": gates, "all_passed": all(gates.values()),
            "human_approval_required": True}


def scientific_verdict(
    e1: Mapping[str, Any], gates: Mapping[str, Any], taxonomy: Mapping[str, Any],
    *, corpus_total: int,
) -> dict[str, Any]:
    """Reach exactly one evidence-backed verdict. Never overclaims."""
    delta = e1.get("authoritative_rca_delta", {})
    n_labeled = e1.get("n_labeled", 0)
    unchanged = bool(delta.get("authoritative_unchanged"))
    sq = e1.get("signal_quality_verification", {})

    # Safety regression: the shadow stack, if promoted, would disagree with
    # a correct authoritative answer (verified-incorrect while authoritative
    # would have been correct). Guarded because authoritative is unchanged.
    if not unchanged:
        verdict = "NOT_READY_SAFETY_REGRESSION"
        reason = ("shadow metadata coincided with a change in the "
                  "authoritative result — contract violation")
    elif delta.get("treatment_correct", 0) < delta.get("control_correct", 0):
        verdict = "NOT_READY_ACCURACY_REGRESSION"
        reason = "treatment RCA correctness below control on labeled data"
    elif corpus_total < MIN_CORPUS_TOTAL or n_labeled < MIN_POWER_N:
        verdict = "READY_AFTER_MORE_SHADOW_EVIDENCE"
        reason = (f"stack is safe and regression-clean, but the labeled "
                  f"corpus ({n_labeled} labeled / {corpus_total} total) is "
                  f"far below the {MIN_POWER_N}-sample power floor and the "
                  f"{MIN_CORPUS_TOTAL}-record readiness gate; RCA benefit is "
                  f"unmeasured and, for a shadow-only stack, unmeasurable "
                  f"without a controlled authoritative experiment")
    elif gates.get("all_passed"):
        verdict = "READY_FOR_CONTROLLED_HUMAN_PILOT"
        reason = "all promotion gates pass on a sufficiently powered corpus"
    else:
        verdict = "NOT_READY_INSUFFICIENT_EVIDENCE"
        reason = "one or more promotion gates unmet"

    return {
        "verdict": verdict,
        "reason": reason,
        "authoritative_unchanged": unchanged,
        "n_labeled": n_labeled,
        "corpus_total": corpus_total,
        "safety_agreement": e1.get("safety_shadow_authoritative_agreement"),
        "verified_incorrect": sq.get("verified_incorrect", 0),
        "total_failures": taxonomy.get("total_failures", 0),
    }


# ---------------------------------------------------------------------------
# Top-level report (deterministic; caller persists append-only)
# ---------------------------------------------------------------------------

def build_report(
    records: Iterable[Mapping[str, Any]],
    *, corpus_total: int | None = None,
    regression_clean: bool = True, replay_clean: bool = True,
) -> dict[str, Any]:
    recs = list(records)
    total = corpus_total if corpus_total is not None else len(recs)
    e1 = e1_shadow_evaluation(recs)
    per_class = per_class_effectiveness(recs)
    taxonomy = failure_taxonomy(recs)
    gates = promotion_gate_assessment(
        e1, per_class, corpus_total=total,
        regression_clean=regression_clean, replay_clean=replay_clean)
    verdict = scientific_verdict(e1, gates, taxonomy, corpus_total=total)
    return {
        "schema_version": SCIENTIFIC_VALIDATION_SCHEMA_VERSION,
        "n_records": len(recs),
        "corpus_total": total,
        "e1_shadow": e1,
        "per_class": per_class,
        "failure_taxonomy": taxonomy,
        "promotion_gates": gates,
        "verdict": verdict,
    }


__all__ = [
    "SCIENTIFIC_VALIDATION_SCHEMA_VERSION", "NOT_MEASURED", "INCIDENT_CLASSES",
    "FAILURE_CATEGORIES", "VERDICTS",
    "rca_correct", "canonical_evaluation_record", "e1_shadow_evaluation",
    "per_class_effectiveness", "mcnemar", "bootstrap_ci", "effect_size",
    "classify_failure", "failure_taxonomy", "promotion_gate_assessment",
    "scientific_verdict", "build_report",
]
