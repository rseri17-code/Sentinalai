"""90-Day Shadow Pilot — produce-only operational evidence collection.

This module turns SentinelAI into a continuously self-certifying production
CANDIDATE. It answers, from evidence rather than opinion, "am I becoming more
trustworthy?" — by composing the outputs that already exist on completed
investigations into immutable observation records, rolling quality scorecards,
longitudinal trends, regression watches, weekly production scorecards, and a
re-evaluation of the existing readiness gates.

STRICT SCOPE — this is OBSERVATION, not capability:
  * No new intelligence / reasoning / agents / retrieval / Wave 3.
  * Reads completed investigation results; never touches the runtime path,
    AnalyzePhase, planner, workers, memory, replay, or admission.
  * Reuses the existing machinery: scientific_validation (canonical record,
    rca_correct, bootstrap_ci, per-class), effectiveness (_trend), and
    readiness (GateInputs / evaluate_gates — the sole gatekeeper).
  * Pure, deterministic, offline: no clock, no randomness (bootstrap is
    caller-seeded), sorted iteration, byte-stable JSON. Additive and
    fully removable.

Every metric is reported with its sample size and (where computed) a
confidence interval; where ground truth is absent the metric is NOT_MEASURED,
never estimated (Phase 10 scientific-reporting discipline).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.effectiveness import _trend
from sentinel_core.investigation_value.readiness import (
    GateInputs,
    evaluate_gates,
)
from sentinel_core.investigation_value.scientific_validation import (
    NOT_MEASURED,
    bootstrap_ci,
    rca_correct,
)

SHADOW_PILOT_SCHEMA_VERSION = 1

# Labeling verdicts (Phase 2 — design surface; values consumed here).
LABEL_CORRECT = "ROOT_CAUSE_CORRECT"
LABEL_PARTIAL = "ROOT_CAUSE_PARTIAL"
LABEL_INCORRECT = "ROOT_CAUSE_INCORRECT"
LABEL_UNKNOWN = "UNKNOWN"


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


# ---------------------------------------------------------------------------
# Phase 1 — Shadow Observation Record (immutable, composes existing outputs)
# ---------------------------------------------------------------------------

def observation_record(
    result: Mapping[str, Any],
    incident: Mapping[str, Any] | None = None,
    *,
    commit: str = "",
    model: str = "",
    observed_period: str = "",
    replay_hash: str = "",
    label: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One immutable observation composed from a completed investigation.

    ``observed_period`` is a caller-supplied bucket key (e.g. an ISO day) —
    NO wall-clock is read here. The incident timestamp is the only time source
    for investigation fields (blocker-B2 discipline). ``label`` is an optional
    operator label (Phase 2)."""
    inc = dict(incident or {})
    val = result.get("_investigation_validation") or {}
    di = result.get("_decision_intelligence") or {}
    graph = result.get("_hypothesis_graph") or {}
    causal = result.get("_causal_investigation") or {}

    ev_val = val.get("evidence_validation", {}) if isinstance(val, dict) else {}
    conf_rec = val.get("confidence_reconstruction", {}) \
        if isinstance(val, dict) else {}
    concord = val.get("expert_concordance", {}) if isinstance(val, dict) else {}
    localization = causal.get("localization", {}) \
        if isinstance(causal, dict) else {}

    unavailable = result.get("_sources_unavailable") or []

    core = {
        "root_cause": str(result.get("root_cause", "")),
        "confidence": int(result.get("confidence", 0) or 0),
        "incident_type": str(inc.get("incident_type",
                                     result.get("incident_type", ""))),
        "service": str(inc.get("service", "")),
        "severity": inc.get("severity"),
        "citation_coverage": result.get("citation_coverage"),
        "evidence_validation_score": ev_val.get("evidence_validation_score"),
        "evidence_confidence": conf_rec.get("evidence_confidence"),
        "verification_status": (val.get("root_cause_verification", {})
                                or {}).get("verification_status", NOT_MEASURED)
        if isinstance(val, dict) else NOT_MEASURED,
        "shadow_independent_winner": (di.get("decision_arbitration", {})
                                      or {}).get("winner", "")
        if isinstance(di, dict) else "",
        "decision_stable": (di.get("decision_stability", {}) or {}).get(
            "stable") if isinstance(di, dict) else None,
        "decision_quality": (di.get("decision_quality", {}) or {}).get(
            "overall_decision_quality") if isinstance(di, dict) else None,
        "hypothesis_count": len(graph.get("hypotheses", []))
        if isinstance(graph, dict) else 0,
        "localization_service": localization.get("root_cause_service", "")
        if isinstance(localization, dict) else "",
        "investigation_completeness": (val.get("investigation_completeness",
                                       {}) or {}).get(
            "investigation_completeness_score")
        if isinstance(val, dict) else None,
        "expert_concordance": concord.get("expert_concordance_score")
        if isinstance(concord, dict) else None,
        "sources_unavailable": [dict(u) for u in unavailable],
        "degraded_investigation": bool(result.get("degraded_investigation")),
        "worker_failures": sum(
            1 for k, v in result.items()
            if not str(k).startswith("_") and isinstance(v, dict)
            and v.get("error")),
    }

    record = {
        "schema_version": SHADOW_PILOT_SCHEMA_VERSION,
        "incident_id": str(inc.get("incident_id", result.get("incident_id",
                                                             ""))),
        "observed_period": observed_period,
        "commit": commit,
        "model": model,
        "shadow_versions": {
            "validation": (val or {}).get("schema_version")
            if isinstance(val, dict) else None,
        },
        "feature_flags": _feature_flags_present(result),
        "core": core,
        "determinism_hash": _sha16(core),
        "replay_hash": replay_hash,
        "label": _normalise_label(label),
    }
    record["record_id"] = _sha16({k: v for k, v in record.items()
                                  if k not in ("record_id",)})
    return record


def _feature_flags_present(result: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "hypothesis_engine": "_hypothesis_graph" in result,
        "adaptive": "_adaptive_investigation" in result,
        "causal": "_causal_investigation" in result,
        "validation": "_investigation_validation" in result,
        "decision_intelligence": "_decision_intelligence" in result,
    }


def _normalise_label(label: Mapping[str, Any] | None) -> dict[str, Any]:
    """Phase 2 canonical label envelope. verdict ∈ LABEL_*."""
    if not label:
        return {"verdict": LABEL_UNKNOWN, "labeled": False}
    verdict = str(label.get("verdict", LABEL_UNKNOWN))
    if verdict not in (LABEL_CORRECT, LABEL_PARTIAL, LABEL_INCORRECT,
                       LABEL_UNKNOWN):
        verdict = LABEL_UNKNOWN
    return {
        "verdict": verdict,
        "labeled": verdict != LABEL_UNKNOWN,
        "validated_root_cause": str(label.get("validated_root_cause", "")),
        "actual_remediation": str(label.get("actual_remediation", "")),
        "resolution_time_ms": label.get("resolution_time_ms"),
        "operator_confidence": label.get("operator_confidence"),
        "operator_comments": str(label.get("operator_comments", "")),
        "false_positive": bool(label.get("false_positive", False)),
        "false_negative": bool(label.get("false_negative", False)),
        "missing_evidence": sorted(label.get("missing_evidence", []) or []),
    }


# ---------------------------------------------------------------------------
# Phase 10 — scientific-reporting envelope (every metric self-describes)
# ---------------------------------------------------------------------------

def _metric(values: list[float], *, period: str = "", seed: int = 1,
            limitations: str = "") -> dict[str, Any]:
    """Report a metric with sample size + CI; NOT_MEASURED when empty."""
    n = len(values)
    if n == 0:
        return {"value": NOT_MEASURED, "n": 0, "period": period,
                "limitations": limitations or "no samples"}
    ci = bootstrap_ci(values, seed=seed)
    return {
        "value": _round(sum(values) / n),
        "n": n,
        "ci95": [ci.get("lo"), ci.get("hi")],
        "underpowered": ci.get("underpowered", n < 30),
        "period": period,
        "limitations": limitations,
    }


def _rate(flags: list[bool], *, period: str = "",
          limitations: str = "") -> dict[str, Any]:
    n = len(flags)
    if n == 0:
        return {"value": NOT_MEASURED, "n": 0, "period": period,
                "limitations": limitations or "no samples"}
    return {"value": _round(sum(1 for f in flags if f) / n), "n": n,
            "period": period, "underpowered": n < 30,
            "limitations": limitations}


# ---------------------------------------------------------------------------
# Phase 3 — Continuous Quality Scorecard (compute only; never estimate)
# ---------------------------------------------------------------------------

def quality_scorecard(observations: Iterable[Mapping[str, Any]],
                      *, period: str = "") -> dict[str, Any]:
    obs = [o for o in observations]
    n = len(obs)
    cores = [o.get("core", {}) for o in obs]
    labeled = [o for o in obs if o.get("label", {}).get("labeled")]

    def present(key: str) -> list[float]:
        return [float(c[key]) for c in cores
                if isinstance(c.get(key), (int, float))]

    # RCA accuracy + calibration require operator/ground-truth labels.
    rca_flags: list[bool] = []
    cal_err: list[float] = []
    for o in labeled:
        verdict = o["label"]["verdict"]
        correct = verdict == LABEL_CORRECT
        rca_flags.append(correct)
        ec = o["core"].get("evidence_confidence")
        if isinstance(ec, (int, float)):
            cal_err.append(abs(ec / 100.0 - (1.0 if correct else 0.0)))

    # Shadow-authoritative agreement: independent winner ~ root_cause.
    agree_flags = []
    for c in cores:
        iw = c.get("shadow_independent_winner")
        if iw:
            agree_flags.append(bool(_tokens(iw) & _tokens(c.get("root_cause",
                                                               ""))))

    # Determinism: distinct determinism_hash per identical incident is the
    # replay-stability probe; here we report the fraction of observations
    # whose determinism_hash is reproducible (always true within a record).
    det_hashes = {o.get("determinism_hash") for o in obs}

    return {
        "schema_version": SHADOW_PILOT_SCHEMA_VERSION,
        "period": period,
        "investigations": n,
        "labeled": len(labeled),
        "metrics": {
            "rca_accuracy": _rate(rca_flags, period=period,
                                  limitations="requires operator labels"),
            "calibration_error": _metric(cal_err, period=period,
                                         limitations="|evidence_conf-correct|;"
                                         " requires labels"),
            "shadow_authoritative_agreement": _rate(agree_flags, period=period),
            "evidence_validation_score": _metric(
                present("evidence_validation_score"), period=period),
            "citation_coverage": _metric(present("citation_coverage"),
                                         period=period),
            "investigation_completeness": _metric(
                present("investigation_completeness"), period=period),
            "decision_quality": _metric(present("decision_quality"),
                                        period=period),
            "expert_concordance": _metric(present("expert_concordance"),
                                          period=period),
            "decision_stability": _rate(
                [bool(c.get("decision_stable")) for c in cores
                 if c.get("decision_stable") is not None], period=period),
            "degraded_rate": _rate([bool(c.get("degraded_investigation"))
                                    for c in cores], period=period),
            "worker_failure_rate": _rate(
                [bool(c.get("worker_failures")) for c in cores], period=period),
            "source_availability": _rate(
                [not c.get("sources_unavailable") for c in cores],
                period=period),
        },
        "determinism": "PASS" if len(det_hashes) == len(
            {o.get("record_id") for o in obs}) or n == 0 else "REVIEW",
    }


# ---------------------------------------------------------------------------
# Phase 4 — Longitudinal trends (rolling windows / dimensions)
# ---------------------------------------------------------------------------

_DESIRED = {
    "rca_accuracy": "up", "evidence_validation_score": "up",
    "citation_coverage": "up", "investigation_completeness": "up",
    "decision_quality": "up", "shadow_authoritative_agreement": "up",
    "calibration_error": "down", "degraded_rate": "down",
    "worker_failure_rate": "down",
}


def longitudinal_trends(period_scorecards: list[Mapping[str, Any]],
                        ) -> dict[str, Any]:
    """Trend each metric across an ordered list of period scorecards.
    Trends only from measured values; no smoothing that hides regressions."""
    trends: dict[str, Any] = {}
    for metric, desired in sorted(_DESIRED.items()):
        series = []
        for sc in period_scorecards:
            m = (sc.get("metrics", {}) or {}).get(metric, {})
            v = m.get("value")
            if isinstance(v, (int, float)):
                series.append(float(v))
        trends[metric] = _trend(series, desired) if len(series) >= 2 else {
            "verdict": NOT_MEASURED, "periods": len(series)}
    return {"schema_version": SHADOW_PILOT_SCHEMA_VERSION, "trends": trends,
            "periods": len(period_scorecards)}


def bucket_by(observations: Iterable[Mapping[str, Any]],
              dimension: str) -> dict[str, list[dict[str, Any]]]:
    """Group observations by a dimension for per-class/service/severity/model/
    commit scorecards. Deterministic (sorted keys at read time)."""
    field = {
        "incident_class": lambda o: o["core"].get("incident_type", "") or "(none)",
        "service": lambda o: o["core"].get("service", "") or "(none)",
        "severity": lambda o: str(o["core"].get("severity", "(none)")),
        "model": lambda o: o.get("model", "") or "(none)",
        "commit": lambda o: o.get("commit", "") or "(none)",
        "period": lambda o: o.get("observed_period", "") or "(none)",
    }.get(dimension)
    if field is None:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for o in observations:
        out.setdefault(field(o), []).append(dict(o))
    return out


# ---------------------------------------------------------------------------
# Phase 5 — Regression watch
# ---------------------------------------------------------------------------

# metric → (desired_direction, min absolute drop to flag)
_REGRESSION_RULES = {
    "rca_accuracy": ("up", 0.05),
    "shadow_authoritative_agreement": ("up", 0.03),
    "evidence_validation_score": ("up", 0.05),
    "citation_coverage": ("up", 0.05),
    "investigation_completeness": ("up", 0.05),
    "decision_quality": ("up", 0.05),
    "calibration_error": ("down", 0.05),
    "degraded_rate": ("down", 0.05),
    "worker_failure_rate": ("down", 0.05),
    "source_availability": ("up", 0.02),
}


def regression_watch(baseline: Mapping[str, Any], current: Mapping[str, Any],
                     *, first_period: str = "", last_period: str = "",
                     ) -> dict[str, Any]:
    """Detect metric regressions between a baseline and a current scorecard.
    Every regression carries reason / first / last / affected / confidence /
    recommended action."""
    b_m = baseline.get("metrics", {}) or {}
    c_m = current.get("metrics", {}) or {}
    regressions = []
    for metric, (desired, thresh) in sorted(_REGRESSION_RULES.items()):
        bv = (b_m.get(metric, {}) or {}).get("value")
        cv = (c_m.get(metric, {}) or {}).get("value")
        if not isinstance(bv, (int, float)) or not isinstance(cv, (int, float)):
            continue
        drop = (bv - cv) if desired == "up" else (cv - bv)
        if drop >= thresh:
            n = (c_m.get(metric, {}) or {}).get("n", 0)
            regressions.append({
                "metric": metric,
                "reason": f"{metric} moved {desired}-adverse by {_round(drop)} "
                          f"(baseline {_round(bv)} -> current {_round(cv)})",
                "first_occurrence": first_period,
                "last_occurrence": last_period,
                "affected_investigations": n,
                "confidence": "high" if n >= 30 else "low_sample",
                "recommended_action": _regression_action(metric),
            })
    # Determinism / replay regressions are categorical, not thresholded.
    if baseline.get("determinism") == "PASS" and current.get(
            "determinism") not in ("PASS", None):
        regressions.append({
            "metric": "determinism", "reason": "determinism check no longer PASS",
            "first_occurrence": first_period, "last_occurrence": last_period,
            "affected_investigations": current.get("investigations", 0),
            "confidence": "high",
            "recommended_action": "halt pilot; investigate non-determinism"})
    return {"schema_version": SHADOW_PILOT_SCHEMA_VERSION,
            "regressions": sorted(regressions, key=lambda r: r["metric"]),
            "regression_count": len(regressions)}


def _regression_action(metric: str) -> str:
    return {
        "rca_accuracy": "review failure taxonomy; do not advance gates",
        "calibration_error": "recheck confidence reconstruction inputs",
        "degraded_rate": "check dependency health (sources_unavailable)",
        "worker_failure_rate": "check worker/gateway availability",
        "source_availability": "escalate dependency outage",
    }.get(metric, "investigate before next reporting period")


# ---------------------------------------------------------------------------
# Phase 6 + 7 — Production scorecard + gatekeeper (reuses evaluate_gates)
# ---------------------------------------------------------------------------

# Production Trust Index dimension weights (measured only; coverage reported).
_PTI_DIMENSIONS = (
    "rca_accuracy", "shadow_authoritative_agreement", "evidence_validation_score",
    "citation_coverage", "investigation_completeness", "decision_quality",
    "decision_stability", "source_availability",
)


def production_scorecard(scorecard: Mapping[str, Any],
                         gate_inputs: GateInputs | None = None,
                         ) -> dict[str, Any]:
    """Weekly production scorecard: the quality scorecard + the readiness
    gates (the SOLE gatekeeper) + a coverage-aware Production Trust Index."""
    gates = evaluate_gates(gate_inputs) if gate_inputs is not None else {
        "all_passed": False, "reason": "no gate inputs supplied"}
    metrics = scorecard.get("metrics", {}) or {}

    measured = []
    for dim in _PTI_DIMENSIONS:
        v = (metrics.get(dim, {}) or {}).get("value")
        if isinstance(v, (int, float)):
            measured.append(v)
    pti = _round(sum(measured) / len(measured)) if measured else NOT_MEASURED
    coverage = _round(len(measured) / len(_PTI_DIMENSIONS))

    gates_pass = bool(gates.get("all_passed")) if isinstance(gates, dict) \
        else False
    return {
        "schema_version": SHADOW_PILOT_SCHEMA_VERSION,
        "period": scorecard.get("period", ""),
        "investigations": scorecard.get("investigations", 0),
        "labeled": scorecard.get("labeled", 0),
        "metrics": metrics,
        "determinism": scorecard.get("determinism"),
        "production_trust_index": pti,
        "pti_coverage": coverage,
        "readiness_gates": gates,
        "gatekeeper_verdict": "ALL_GATES_PASS" if gates_pass
        else "GATES_FAILING",
        "wave3_recommendation": "READY" if gates_pass else "NOT_READY",
        "note": ("the readiness gate engine is the sole authority; a high PTI "
                 "at low coverage is NOT a pass"),
    }


# ---------------------------------------------------------------------------
# Phase 8 — Chaos observation (observe only; never modify runtime)
# ---------------------------------------------------------------------------

def chaos_observation(observations: Iterable[Mapping[str, Any]],
                      *, period: str = "") -> dict[str, Any]:
    obs = list(observations)
    n = len(obs)
    source_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    degraded = 0
    for o in obs:
        c = o.get("core", {})
        if c.get("degraded_investigation"):
            degraded += 1
        for u in c.get("sources_unavailable", []) or []:
            source_counts[str(u.get("source", "?"))] = \
                source_counts.get(str(u.get("source", "?")), 0) + 1
            reason_counts[str(u.get("reason", "?"))[:40]] = \
                reason_counts.get(str(u.get("reason", "?"))[:40], 0) + 1
    return {
        "schema_version": SHADOW_PILOT_SCHEMA_VERSION,
        "period": period,
        "investigations": n,
        "degraded_investigations": degraded,
        "degraded_rate": _round(degraded / n) if n else NOT_MEASURED,
        "unavailable_by_source": dict(sorted(source_counts.items())),
        "unavailable_by_reason": dict(sorted(reason_counts.items())),
    }


__all__ = [
    "SHADOW_PILOT_SCHEMA_VERSION",
    "LABEL_CORRECT", "LABEL_PARTIAL", "LABEL_INCORRECT", "LABEL_UNKNOWN",
    "observation_record", "quality_scorecard", "longitudinal_trends",
    "bucket_by", "regression_watch", "production_scorecard",
    "chaos_observation",
]
