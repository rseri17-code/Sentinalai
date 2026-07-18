"""Gold Standard Investigation Dataset + Investigation Quality Score (IQS).

The authoritative benchmark SentinelAI must beat before any capability is
promoted. Every completed investigation becomes an immutable benchmark
artifact that supports deterministic, reproducible comparison between:
authoritative runtime · Investigation Intelligence shadow stack · human
investigator · validated postmortem.

Produce-only, offline, deterministic, replayable, removable. Composes
existing outputs (no recomputation, no reasoning). Changes no runtime path,
no authority, no Wave 3, no retrieval. Imported by tests + offline reporting.

Every metric is computed deterministically and reported with its sample size,
a bootstrap CI (caller-seeded — no RNG, no clock), stated limitations, and
NOT_MEASURED wherever ground truth is absent. The single Investigation Quality
Score (IQS) is composed ONLY from validated (measured) metrics, and always
travels with its coverage — a high IQS at low coverage is not a pass.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.scientific_validation import (
    NOT_MEASURED,
    bootstrap_ci,
    rca_correct,
)

GOLD_STANDARD_SCHEMA_VERSION = 1


def _round(x: float) -> float:
    return round(float(x), 4)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha16(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()[:16]


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
# Gold Standard Investigation Record (immutable benchmark artifact)
# ---------------------------------------------------------------------------

def gold_record(
    result: Mapping[str, Any],
    incident: Mapping[str, Any] | None = None,
    *,
    evidence_sequence: list[str] | None = None,
    human: Mapping[str, Any] | None = None,
    postmortem: Mapping[str, Any] | None = None,
    commit: str = "",
    model: str = "",
    replay_hash: str = "",
) -> dict[str, Any]:
    """One immutable benchmark artifact composed from a completed
    investigation plus optional human / postmortem ground truth.

    ``evidence_sequence`` is the caller-supplied acquisition order (for
    decisive-evidence latency); if absent, latency falls back to a
    deterministic sorted order and is marked as such.
    """
    inc = dict(incident or {})
    graph = result.get("_hypothesis_graph") or {}
    narr = result.get("_elimination_narrative") or {}
    val = result.get("_investigation_validation") or {}
    di = result.get("_decision_intelligence") or {}
    causal = result.get("_causal_investigation") or {}
    adaptive = result.get("_adaptive_investigation") or {}

    hyps = graph.get("hypotheses", []) if isinstance(graph, dict) else []
    attribution = (di.get("evidence_attribution") or {}) \
        if isinstance(di, dict) else {}
    snap = result.get("_evidence_snapshot") or {}
    collected = sorted(k for k, v in snap.items()
                       if v and not str(k).startswith("_")) \
        if isinstance(snap, dict) else []

    record = {
        "schema_version": GOLD_STANDARD_SCHEMA_VERSION,
        "incident_id": str(inc.get("incident_id",
                                   result.get("incident_id", ""))),
        "incident_type": str(inc.get("incident_type",
                                     result.get("incident_type", ""))),
        "service": str(inc.get("service", "")),
        "commit": commit, "model": model,
        # --- captured investigation (composed) ---
        "authoritative": {
            "root_cause": str(result.get("root_cause", "")),
            "confidence": int(result.get("confidence", 0) or 0),
        },
        "hypotheses": [
            {"name": str(h.get("name", "")), "status": str(h.get("status", "")),
             "confidence": int(h.get("confidence", 0) or 0),
             "supporting": sorted(str(e.get("key", "")) for e in
                                  h.get("supporting_evidence", []) if e.get("key")),
             "refuting": sorted(str(e.get("key", "")) for e in
                                h.get("refuting_evidence", []) if e.get("key"))}
            for h in hyps if isinstance(h, dict)],
        "eliminated": [dict(x) for x in (narr.get("ruled_out", []) or [])
                       if isinstance(x, dict)],
        "winner": str(narr.get("winner", "")) if isinstance(narr, dict) else "",
        "survived_disconfirmation": bool(narr.get("survived_disconfirmation"))
        if isinstance(narr, dict) else False,
        "evidence_collected": collected,
        "evidence_sequence": list(evidence_sequence) if evidence_sequence
        else collected,
        "evidence_sequence_is_true_order": bool(evidence_sequence),
        "evidence_attribution": {
            "decisive": sorted(attribution.get("decisive_evidence", []) or []),
            "importance_ranking": list(attribution.get("importance_ranking",
                                                       []) or []),
        },
        "localization": (causal.get("localization", {}) or {}).get(
            "root_cause_service", "") if isinstance(causal, dict) else "",
        "counterfactual": str(result.get("_counterfactual", "")),
        "counterfactual_residual": (val.get("counterfactual", {}) or {}).get(
            "counterfactual_residual_score") if isinstance(val, dict) else None,
        "confidence_evolution": {
            "raw": (val.get("confidence_reconstruction", {}) or {}).get(
                "raw_confidence") if isinstance(val, dict) else None,
            "calibrated": int(result.get("confidence", 0) or 0),
            "evidence_derived": (val.get("confidence_reconstruction", {})
                                 or {}).get("evidence_confidence")
            if isinstance(val, dict) else None,
        },
        "verification_status": (val.get("root_cause_verification", {})
                                or {}).get("verification_status", NOT_MEASURED)
        if isinstance(val, dict) else NOT_MEASURED,
        "investigation_completeness": (val.get("investigation_completeness", {})
                                       or {}).get(
            "investigation_completeness_score") if isinstance(val, dict) else None,
        "next_best_evidence": [
            e.get("missing_evidence", []) for e in
            (adaptive.get("next_best_evidence", []) or [])[:1]
            if isinstance(e, dict)],
        # --- operator + validated ground truth ---
        "operator": _operator_block(human),
        "remediation": str(result.get("remediation", "")
                           or (result.get("proposed_fix", {}) or {}).get(
                               "summary", "")) if isinstance(
                                   result.get("proposed_fix"), dict) else str(
                                       result.get("remediation", "")),
        "validated_rca": str((postmortem or {}).get("root_cause",
                             (human or {}).get("validated_root_cause", ""))),
        "postmortem": _postmortem_block(postmortem),
        "replay_hash": replay_hash,
    }
    record["determinism_hash"] = _sha16({
        "root_cause": record["authoritative"]["root_cause"],
        "hypotheses": record["hypotheses"],
        "localization": record["localization"]})
    record["record_id"] = _sha16({k: v for k, v in record.items()
                                  if k != "record_id"})
    return record


def _operator_block(human: Mapping[str, Any] | None) -> dict[str, Any]:
    if not human:
        return {"present": False, "interventions": [], "agreed": None}
    return {
        "present": True,
        "validated_root_cause": str(human.get("validated_root_cause", "")),
        "interventions": sorted(str(x) for x in
                                (human.get("interventions", []) or [])),
        "operator_confidence": human.get("operator_confidence"),
        "agreed": human.get("agreed"),
        "corrections": sorted(str(x) for x in
                              (human.get("corrections", []) or [])),
    }


def _postmortem_block(pm: Mapping[str, Any] | None) -> dict[str, Any]:
    if not pm:
        return {"present": False}
    return {
        "present": True,
        "root_cause": str(pm.get("root_cause", "")),
        "root_cause_keywords": sorted(str(k) for k in
                                      (pm.get("root_cause_keywords", []) or [])),
        "resolution_time_ms": pm.get("resolution_time_ms"),
        "outcome": str(pm.get("outcome", "")),
    }


# ---------------------------------------------------------------------------
# Deterministic per-record metrics
# ---------------------------------------------------------------------------

def _ground_truth(record: Mapping[str, Any]) -> dict[str, Any]:
    """Prefer postmortem, else operator-validated, as the ground truth."""
    pm = record.get("postmortem", {})
    if pm.get("present"):
        return {"root_cause": pm.get("root_cause", ""),
                "root_cause_keywords": pm.get("root_cause_keywords", [])}
    op = record.get("operator", {})
    if op.get("present") and op.get("validated_root_cause"):
        return {"root_cause": op["validated_root_cause"]}
    return {}


def record_metrics(record: Mapping[str, Any]) -> dict[str, Any]:
    """All investigation-quality metrics for one record. Values are floats in
    [0,1] (or None => NOT_MEASURED at aggregation)."""
    hyps = record.get("hypotheses", [])
    n_hyps = len(hyps)
    collected = record.get("evidence_collected", [])
    total_ev = len(collected)
    decisive = record.get("evidence_attribution", {}).get("decisive", [])
    gt = _ground_truth(record)
    correct = rca_correct(record.get("authoritative", {}).get("root_cause", ""),
                          gt)

    # hypothesis efficiency: fewer hypotheses to a confirmed winner = better.
    hyp_eff = _round(1.0 / n_hyps) if n_hyps else None

    # evidence efficiency: fraction of collected evidence that was decisive or
    # attached to a hypothesis (signal density).
    attached = set()
    for h in hyps:
        attached |= set(h.get("supporting", [])) | set(h.get("refuting", []))
    useful = attached | set(decisive)
    ev_eff = _round(len(useful & set(collected)) / total_ev) \
        if total_ev else None
    unnecessary = _round(1.0 - (len(useful & set(collected)) / total_ev)) \
        if total_ev else None

    # decisive-evidence latency: normalized position of the first decisive
    # item in the acquisition sequence (earlier = better -> lower is better,
    # reported as 1 - normalized_index so higher = better).
    seq = record.get("evidence_sequence", [])
    latency_score = None
    if decisive and seq:
        idxs = [seq.index(d) for d in decisive if d in seq]
        if idxs:
            latency_score = _round(1.0 - (min(idxs) / max(1, len(seq) - 1))) \
                if len(seq) > 1 else 1.0

    # false-lead rate: hypotheses carrying refuting evidence (pursued then
    # refuted) / total. Lower is better -> reported as 1 - rate.
    false_leads = sum(1 for h in hyps if h.get("refuting"))
    false_lead_score = _round(1.0 - false_leads / n_hyps) if n_hyps else None

    # localization accuracy: localized service overlaps the validated cause.
    loc = record.get("localization", "")
    loc_acc = None
    if loc and gt.get("root_cause"):
        loc_l = loc.strip().lower()
        svc_l = record.get("service", "").strip().lower()
        # substring match handles short service names (e.g. "db") that the
        # >=3-char token floor would otherwise drop.
        loc_acc = 1.0 if (
            _overlap(loc, gt["root_cause"])
            or (loc_l and loc_l in gt["root_cause"].lower())
            or (loc_l and svc_l and loc_l == svc_l)) else 0.0

    # confidence calibration: 1 - |evidence_confidence/100 - correct|.
    cal = None
    ec = record.get("confidence_evolution", {}).get("evidence_derived")
    if isinstance(ec, (int, float)) and correct is not None:
        cal = _round(1.0 - abs(ec / 100.0 - (1.0 if correct else 0.0)))

    # completeness (T4) already in [0,1].
    comp = record.get("investigation_completeness")
    comp = float(comp) if isinstance(comp, (int, float)) else None

    # operator agreement: authoritative RCA vs operator-validated RCA.
    op = record.get("operator", {})
    op_agree = None
    if op.get("present") and op.get("validated_root_cause"):
        op_agree = 1.0 if _overlap(
            record.get("authoritative", {}).get("root_cause", ""),
            op["validated_root_cause"]) else 0.0

    # replay fidelity: a stable replay hash present = reproducible artifact.
    replay = 1.0 if record.get("replay_hash") else None

    return {
        "hypothesis_efficiency": hyp_eff,
        "evidence_efficiency": ev_eff,
        "unnecessary_evidence_avoided": unnecessary,
        "decisive_evidence_latency": latency_score,
        "false_lead_avoidance": false_lead_score,
        "localization_accuracy": loc_acc,
        "confidence_calibration": cal,
        "investigation_completeness": comp,
        "operator_agreement": op_agree,
        "replay_fidelity": replay,
    }


# ---------------------------------------------------------------------------
# Aggregate evaluation + Investigation Quality Score
# ---------------------------------------------------------------------------

_METRIC_LIMITS = {
    "hypothesis_efficiency": "1/hypothesis_count; assumes a confirmed winner",
    "evidence_efficiency": "signal density; not correctness",
    "unnecessary_evidence_avoided": "complement of signal density",
    "decisive_evidence_latency": "needs true acquisition order (else sorted "
                                 "fallback, not real latency)",
    "false_lead_avoidance": "counts refuted hypotheses; a good pursuit may "
                            "legitimately refute many",
    "localization_accuracy": "requires validated ground truth",
    "confidence_calibration": "requires labels; underpowered below n=30",
    "investigation_completeness": "evidence-category coverage, not correctness",
    "operator_agreement": "requires operator-validated RCA",
    "replay_fidelity": "presence of a stable replay hash, not a re-run",
}

# IQS weights — validated-metrics only, normalized over the measured subset.
_IQS_WEIGHTS = {
    "localization_accuracy": 0.18,
    "confidence_calibration": 0.15,
    "operator_agreement": 0.15,
    "evidence_efficiency": 0.10,
    "decisive_evidence_latency": 0.10,
    "false_lead_avoidance": 0.10,
    "hypothesis_efficiency": 0.07,
    "investigation_completeness": 0.07,
    "unnecessary_evidence_avoided": 0.05,
    "replay_fidelity": 0.03,
}


def _metric(values: list[float], *, seed: int, limitations: str) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"value": NOT_MEASURED, "n": 0, "limitations": limitations}
    ci = bootstrap_ci(values, seed=seed)
    return {"value": _round(sum(values) / n), "n": n,
            "ci95": [ci.get("lo"), ci.get("hi")],
            "underpowered": ci.get("underpowered", n < 30),
            "limitations": limitations}


def evaluate_dataset(records: Iterable[Mapping[str, Any]],
                     *, seed: int = 1) -> dict[str, Any]:
    """Compute every metric across the dataset + the single IQS. Metrics with
    no measured samples are NOT_MEASURED and excluded from the IQS; coverage
    is reported alongside."""
    recs = list(records)
    per: dict[str, list[float]] = {k: [] for k in _METRIC_LIMITS}
    for r in recs:
        m = record_metrics(r)
        for k, v in m.items():
            if isinstance(v, (int, float)):
                per[k].append(float(v))

    metrics = {k: _metric(per[k], seed=seed + i,
                          limitations=_METRIC_LIMITS[k])
               for i, k in enumerate(sorted(_METRIC_LIMITS))}

    # IQS from validated (measured) metrics only, weights renormalized.
    measured = {k: m for k, m in metrics.items()
                if isinstance(m["value"], (int, float))}
    total_w = sum(_IQS_WEIGHTS[k] for k in measured) or 1.0
    iqs = _round(sum(_IQS_WEIGHTS[k] * measured[k]["value"]
                     for k in measured) / total_w) if measured else NOT_MEASURED
    coverage = _round(len(measured) / len(_IQS_WEIGHTS))

    return {
        "schema_version": GOLD_STANDARD_SCHEMA_VERSION,
        "n_records": len(recs),
        "metrics": metrics,
        "investigation_quality_score": iqs,
        "iqs_coverage": coverage,
        "iqs_note": ("IQS composed only from validated (measured) metrics; a "
                     "high IQS at low coverage is not a pass — both travel "
                     "together"),
    }


__all__ = [
    "GOLD_STANDARD_SCHEMA_VERSION",
    "gold_record", "record_metrics", "evaluate_dataset",
]
