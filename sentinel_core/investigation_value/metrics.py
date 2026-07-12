"""Investigation Improvement metrics — the audit's Phase-3 instruments.

Seven deterministic, offline, closed-form metrics. Every function is a
pure transform of caller-supplied values extracted from Investigation
Artifacts and MemoryRecords. No I/O, no clock, no randomness, no LLM.

Conventions (from the Investigation Value Audit):
- query investigation *q* — the investigation being scored
- retrieved memory set *M* — records retrieval WOULD have surfaced
- baseline *B* — trailing same-incident_type window without retrieval
All scores round to 4 dp; negative-capable scores clamp at -1.0.
"""
from __future__ import annotations

from typing import Any, Iterable

METRICS_SCHEMA_VERSION = 1


def _tokens(s: str) -> set[str]:
    """Case-folded ≥3-char token set (same policy as SimilarityEngine)."""
    if not s:
        return set()
    out: set[str] = set()
    for tok in str(s).lower().split():
        t = tok.strip(".,;:()[]!?\"'`<>-")
        if len(t) >= 3:
            out.add(t)
    return out


def _jaccard(a: set[Any], b: set[Any]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# IIP — Investigation Improvement Potential
# ---------------------------------------------------------------------------

def investigation_improvement_potential(
    retrieved_root_causes: Iterable[str],
    confirmed_root_cause: str,
    retrieved_evidence: Iterable[str],
    decisive_evidence: Iterable[str],
    retrieved_false_leads: Iterable[str],
    ruled_out_hypotheses: Iterable[str],
    w_r: float = 0.5, w_e: float = 0.3, w_f: float = 0.2,
) -> float:
    """IIP = w_r·RCM + w_e·EO + w_f·FLC ∈ [0, 1].

    RCM: best token-jaccard between any retrieved root cause and the
    confirmed one. EO: fraction of decisive evidence already named by
    retrieval. FLC: fraction of ruled-out hypotheses retrieval had
    flagged as false leads. ≥ 0.6 ⇒ retrieval would have materially
    helped. Producer: nightly job. Consumer: gate G6.
    """
    confirmed = _tokens(confirmed_root_cause)
    rcm = 0.0
    for rc in retrieved_root_causes:
        rcm = max(rcm, _jaccard(_tokens(rc), confirmed))

    decisive = set(str(x) for x in decisive_evidence)
    if decisive:
        got = set(str(x) for x in retrieved_evidence)
        eo = len(decisive & got) / len(decisive)
    else:
        eo = 0.0

    ruled_out = set(_t for h in ruled_out_hypotheses for _t in _tokens(h))
    flagged = set(_t for f in retrieved_false_leads for _t in _tokens(f))
    if ruled_out:
        flc = len(ruled_out & flagged) / len(ruled_out)
    else:
        flc = 0.0

    return round(w_r * rcm + w_e * eo + w_f * flc, 4)


# ---------------------------------------------------------------------------
# WRS — Worker Reduction Score
# ---------------------------------------------------------------------------

def worker_reduction_score(calls_q: int, baseline_median_calls: float) -> float:
    """WRS = 1 − calls(q)/median(B) ∈ [-1, 1] (clamped).

    > 0 ⇒ fewer worker calls than baseline; < 0 ⇒ memory made it worse.
    Producer: shadow comparison. Consumer: gate G7.
    """
    if baseline_median_calls <= 0:
        return 0.0
    return round(_clamp(1.0 - float(calls_q) / float(baseline_median_calls)), 4)


# ---------------------------------------------------------------------------
# EAS — Evidence Acceleration Score
# ---------------------------------------------------------------------------

def evidence_acceleration_score(
    rank_first_decisive_baseline: int,
    rank_first_decisive_q: int,
    playbook_length: int,
) -> float:
    """EAS = (rank_B − rank_q) / playbook_length ∈ [-1, 1].

    > 0 ⇒ decisive evidence reached earlier than baseline. Requires REAL
    evidence ordering (B3) — never computed over fabricated sequences.
    Producer: nightly. Consumer: gate G6 support; evolver seed quality.
    """
    if playbook_length <= 0:
        return 0.0
    delta = (int(rank_first_decisive_baseline) - int(rank_first_decisive_q))
    return round(_clamp(delta / float(playbook_length)), 4)


# ---------------------------------------------------------------------------
# FLES — False Lead Elimination Score
# ---------------------------------------------------------------------------

def false_lead_elimination_score(
    ruled_out_early: int, red_herrings_present: int,
) -> float:
    """FLES = ruled_out_early / red_herrings_present ∈ [0, 1].

    Fraction of known red herrings never pursued (demoted before LLM
    synthesis). Producer: nightly. Consumer: hypothesis-priming gate.
    """
    if red_herrings_present <= 0:
        return 0.0
    return round(_clamp(ruled_out_early / float(red_herrings_present),
                        0.0, 1.0), 4)


# ---------------------------------------------------------------------------
# PGS — Planner Guidance Score
# ---------------------------------------------------------------------------

def planner_guidance_score(
    recommended_steps: Iterable[str],
    productive_steps: Iterable[str],
) -> float:
    """PGS = |recommended ∩ productive| / |recommended| ∈ [0, 1].

    Precision of memory's ordering advice; productive = step whose
    evidence was cited by the confirmed RCA. < 0.5 ⇒ advice is noise.
    Producer: nightly. Consumer: gate G6; GUIDED_INVESTIGATION flip.
    """
    rec = set(str(x) for x in recommended_steps)
    if not rec:
        return 0.0
    good = set(str(x) for x in productive_steps)
    return round(len(rec & good) / len(rec), 4)


# ---------------------------------------------------------------------------
# CG — Confidence Gain
# ---------------------------------------------------------------------------

def confidence_gain(
    baseline_calibration_error: float,
    with_memory_calibration_error: float,
) -> float:
    """CG = err(B) − err(with memory) ∈ [-100, 100].

    Errors are |confidence − outcome| where outcome ∈ {0, 100} from
    validation. > 0 ⇒ memory-informed confidence is better calibrated.
    Producer: nightly calibration pass. Consumer: gate G9.
    """
    return round(_clamp(
        float(baseline_calibration_error) - float(with_memory_calibration_error),
        -100.0, 100.0), 4)


# ---------------------------------------------------------------------------
# RCAS — Root Cause Acceleration Score
# ---------------------------------------------------------------------------

def root_cause_acceleration_score(
    baseline_median_mtti_ms: float, mtti_q_ms: int,
) -> float:
    """RCAS = (median_mtti(B) − mtti(q)) / median_mtti(B) ∈ [-1, 1].

    The headline number: fractional MTTI reduction attributable to
    memory, per incident_type. Producer: nightly. Consumers: G6/G7 and
    the H1 hypothesis measurement.
    """
    if baseline_median_mtti_ms <= 0:
        return 0.0
    return round(_clamp(
        (float(baseline_median_mtti_ms) - float(mtti_q_ms))
        / float(baseline_median_mtti_ms)), 4)


__all__ = [
    "METRICS_SCHEMA_VERSION",
    "confidence_gain",
    "evidence_acceleration_score",
    "false_lead_elimination_score",
    "investigation_improvement_potential",
    "planner_guidance_score",
    "root_cause_acceleration_score",
    "worker_reduction_score",
]
