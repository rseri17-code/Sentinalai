"""Enterprise Investigation Challenge (EIC) — engine-agnostic benchmark core.

The permanent, engine-independent benchmark for incident investigation — the
SWE-bench / MMLU / ImageNet analogue for enterprise RCA. It scores ANY
investigator (SentinelAI, a human Principal SRE, Dynatrace Davis, a research
agent, a future engine) against a standard task, from a NEUTRAL submission
format that contains no engine-specific fields.

DESIGN INVARIANT — engine agnosticism: this module imports nothing from the
SentinelAI runtime and reads none of its internal ``_*`` shadow keys. It
operates purely on two plain dicts — a Task and a Submission — so the same
scorer grades every engine identically. The only SentinelAI-coupled code is
the optional adapter (eic/adapter.py) that converts a SentinelAI result into
a neutral Submission; the benchmark does not depend on it.

Produce-only, offline, deterministic, replayable, removable. Reuses only
generic scoring utilities (rca_correct, bootstrap_ci) from
scientific_validation. Does NOT modify IQS or the Gold Dataset.
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

EIC_SCHEMA_VERSION = 1

CATEGORIES = (
    "kubernetes", "database", "deployment", "authentication", "dns",
    "middleware", "network", "storage", "cloud", "multi_cause",
    "cascading_failure",
)

# novice -> expert; each names the investigative challenge it exercises.
DIFFICULTY = (
    "single_cause", "competing_hypotheses", "missing_telemetry",
    "contradictory_evidence", "cross_service", "cascading_failure",
    "unknown_root_cause",
)


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


def _service_match(claimed: str, truth_service: str, truth_rc: str) -> bool:
    c = str(claimed or "").strip().lower()
    if not c:
        return False
    if truth_service and c == str(truth_service).strip().lower():
        return True
    if truth_service and c in str(truth_service).strip().lower():
        return True
    return bool(truth_rc and c in str(truth_rc).lower()) or _overlap(
        claimed, truth_rc)


# ---------------------------------------------------------------------------
# Schemas (builders that normalize + stamp a deterministic id)
# ---------------------------------------------------------------------------

def make_task(
    *,
    task_id: str,
    category: str,
    difficulty: str,
    incident: Mapping[str, Any],
    telemetry: Mapping[str, Any],
    ground_truth: Mapping[str, Any],
    traps: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One engine-agnostic benchmark task. ``telemetry`` is opaque evidence
    keyed by name (an engine reads whatever it wants). ``ground_truth`` and
    ``traps`` are the hidden answer key + distractors used only by the scorer.
    """
    traps = traps or {}
    task = {
        "schema_version": EIC_SCHEMA_VERSION,
        "task_id": str(task_id),
        "category": str(category),
        "difficulty": str(difficulty),
        "incident": dict(incident),
        "telemetry_keys": sorted(str(k) for k in telemetry),
        "telemetry": {str(k): telemetry[k] for k in telemetry},
        "ground_truth": {
            "root_cause": str(ground_truth.get("root_cause", "")),
            "root_cause_keywords": sorted(
                str(k) for k in ground_truth.get("root_cause_keywords", [])),
            "root_cause_service": str(ground_truth.get("root_cause_service", "")),
            "necessary_evidence": sorted(
                str(k) for k in ground_truth.get("necessary_evidence", [])),
            "decisive_evidence": sorted(
                str(k) for k in ground_truth.get("decisive_evidence", [])),
        },
        "traps": {
            "distractor_evidence": sorted(
                str(k) for k in traps.get("distractor_evidence", [])),
            "false_hypotheses": sorted(
                str(k) for k in traps.get("false_hypotheses", [])),
        },
    }
    task["task_hash"] = _sha16({k: v for k, v in task.items()
                                if k != "task_hash"})
    return task


def make_submission(
    *,
    engine: str,
    engine_version: str = "",
    task_id: str,
    root_cause: str,
    localized_service: str = "",
    hypotheses: Iterable[str] = (),
    ruled_out: Iterable[str] = (),
    evidence_used: Iterable[str] = (),
    decisive_evidence: Iterable[str] = (),
    confidence: int = 0,
    proof: str = "",
    replay_hash: str = "",
) -> dict[str, Any]:
    """A neutral investigation result any engine can emit. ``evidence_used``
    is the ORDERED acquisition sequence (for decisive-evidence latency)."""
    return {
        "schema_version": EIC_SCHEMA_VERSION,
        "engine": str(engine),
        "engine_version": str(engine_version),
        "task_id": str(task_id),
        "root_cause": str(root_cause),
        "localized_service": str(localized_service),
        "hypotheses": [str(h) for h in hypotheses],
        "ruled_out": [str(h) for h in ruled_out],
        "evidence_used": [str(e) for e in evidence_used],
        "decisive_evidence": [str(e) for e in decisive_evidence],
        "confidence": int(confidence or 0),
        "proof": str(proof),
        "replay_hash": str(replay_hash),
    }


# ---------------------------------------------------------------------------
# Deterministic scorer (operates ONLY on task + submission)
# ---------------------------------------------------------------------------

# dimension -> weight in the composite EIC score
_EIC_WEIGHTS = {
    "rca_correctness": 0.30,
    "localization": 0.15,
    "false_lead_avoidance": 0.12,
    "decisive_evidence_latency": 0.10,
    "evidence_efficiency": 0.10,
    "distractor_avoidance": 0.08,
    "hypothesis_quality": 0.05,
    "confidence_calibration": 0.05,
    "explainability": 0.03,
    "replayability": 0.02,
}


def score_submission(task: Mapping[str, Any],
                     submission: Mapping[str, Any]) -> dict[str, Any]:
    """Grade one submission against one task. Every dimension in [0,1] or None
    (=> NOT_MEASURED, excluded from the composite)."""
    gt = task.get("ground_truth", {})
    traps = task.get("traps", {})
    rc = str(submission.get("root_cause", ""))

    correct = rca_correct(rc, gt)
    rca_score = None if correct is None else (1.0 if correct else 0.0)

    # localization
    loc = str(submission.get("localized_service", ""))
    loc_score = None
    if gt.get("root_cause_service") or gt.get("root_cause"):
        loc_score = 1.0 if _service_match(
            loc, gt.get("root_cause_service", ""),
            gt.get("root_cause", "")) else 0.0

    used = list(submission.get("evidence_used", []))
    used_set = set(used)
    necessary = set(gt.get("necessary_evidence", []))
    distractors = set(traps.get("distractor_evidence", []))
    false_hyps = set(traps.get("false_hypotheses", []))

    # evidence efficiency: precision of collected evidence vs necessary set
    ev_eff = None
    if used_set:
        ev_eff = _round(len(used_set & necessary) / len(used_set)) \
            if necessary else None

    # distractor avoidance: did the engine steer clear of the traps?
    distractor_avoid = None
    if distractors:
        distractor_avoid = _round(
            1.0 - len(used_set & distractors) / len(distractors))

    # false-lead avoidance: did it rule out the plausible-wrong hypotheses?
    ruled = {h.lower() for h in submission.get("ruled_out", [])}
    fl_avoid = None
    if false_hyps:
        hit = sum(1 for f in false_hyps
                  if any(_overlap(f, r) or f.lower() in r
                         for r in ruled))
        fl_avoid = _round(hit / len(false_hyps))

    # decisive-evidence latency: earliest collection = best; never collected = 0
    decisive = list(gt.get("decisive_evidence", []))
    lat = None
    if decisive:
        idxs = [used.index(d) for d in decisive if d in used]
        if not idxs:
            lat = 0.0
        elif len(used) > 1:
            lat = _round(1.0 - min(idxs) / (len(used) - 1))
        else:
            lat = 1.0

    # hypothesis quality: was the true cause among the considered hypotheses?
    hyps = list(submission.get("hypotheses", []))
    hyp_q = None
    if gt.get("root_cause") and hyps:
        hyp_q = 1.0 if any(_overlap(h, gt["root_cause"]) for h in hyps) else 0.0

    # confidence calibration
    cal = None
    if correct is not None:
        conf = int(submission.get("confidence", 0) or 0)
        cal = _round(1.0 - abs(conf / 100.0 - (1.0 if correct else 0.0)))

    # explainability: proof present and grounded in >=1 named evidence key
    proof = str(submission.get("proof", ""))
    expl = None
    if proof:
        grounded = any(k in proof for k in task.get("telemetry_keys", []))
        expl = 1.0 if grounded else 0.5
    elif proof == "":
        expl = 0.0

    # replayability
    replay = 1.0 if submission.get("replay_hash") else 0.0

    dims = {
        "rca_correctness": rca_score,
        "localization": loc_score,
        "false_lead_avoidance": fl_avoid,
        "decisive_evidence_latency": lat,
        "evidence_efficiency": ev_eff,
        "distractor_avoidance": distractor_avoid,
        "hypothesis_quality": hyp_q,
        "confidence_calibration": cal,
        "explainability": expl,
        "replayability": replay,
    }

    measured = {k: v for k, v in dims.items() if isinstance(v, (int, float))}
    total_w = sum(_EIC_WEIGHTS[k] for k in measured) or 1.0
    eic = _round(sum(_EIC_WEIGHTS[k] * measured[k]
                     for k in measured) / total_w) if measured else NOT_MEASURED
    coverage = _round(len(measured) / len(_EIC_WEIGHTS))

    return {
        "schema_version": EIC_SCHEMA_VERSION,
        "task_id": task.get("task_id"),
        "engine": submission.get("engine"),
        "engine_version": submission.get("engine_version"),
        "category": task.get("category"),
        "difficulty": task.get("difficulty"),
        "dimensions": {k: (v if v is not None else NOT_MEASURED)
                       for k, v in dims.items()},
        "eic_score": eic,
        "coverage": coverage,
    }


# ---------------------------------------------------------------------------
# Longitudinal leaderboard
# ---------------------------------------------------------------------------

def _mean(vals: list[float]) -> Any:
    return _round(sum(vals) / len(vals)) if vals else NOT_MEASURED


def leaderboard(scores: Iterable[Mapping[str, Any]], *,
                release: str = "", seed: int = 1) -> dict[str, Any]:
    """Rank engines by mean EIC score, with per-category / per-difficulty
    breakdowns + CI. Deterministic; ``release`` tags a longitudinal entry."""
    by_engine: dict[str, list[Mapping[str, Any]]] = {}
    for s in scores:
        by_engine.setdefault(str(s.get("engine", "?")), []).append(s)

    rows = []
    for engine in sorted(by_engine):
        ss = by_engine[engine]
        vals = [s["eic_score"] for s in ss
                if isinstance(s.get("eic_score"), (int, float))]
        ci = bootstrap_ci(vals, seed=seed) if vals else {}
        per_cat: dict[str, Any] = {}
        per_diff: dict[str, Any] = {}
        for s in ss:
            if isinstance(s.get("eic_score"), (int, float)):
                per_cat.setdefault(str(s.get("category")), []).append(
                    s["eic_score"])
                per_diff.setdefault(str(s.get("difficulty")), []).append(
                    s["eic_score"])
        rows.append({
            "engine": engine,
            "tasks_scored": len(ss),
            "eic_score_mean": _mean(vals),
            "ci95": [ci.get("lo"), ci.get("hi")] if vals else NOT_MEASURED,
            "underpowered": (len(vals) < 30) if vals else True,
            "by_category": {c: _mean(v) for c, v in sorted(per_cat.items())},
            "by_difficulty": {d: _mean(v) for d, v in sorted(per_diff.items())},
        })
    ranked = sorted(
        rows, key=lambda r: (-(r["eic_score_mean"] if isinstance(
            r["eic_score_mean"], (int, float)) else -1), r["engine"]))
    return {
        "schema_version": EIC_SCHEMA_VERSION,
        "release": release,
        "engines": ranked,
        "leader": ranked[0]["engine"] if ranked else NOT_MEASURED,
    }


__all__ = [
    "EIC_SCHEMA_VERSION", "CATEGORIES", "DIFFICULTY",
    "make_task", "make_submission", "score_submission", "leaderboard",
]
