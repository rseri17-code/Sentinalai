"""SentinelBench scoring — 8 deterministic dimensions + weighted overall.

Every scoring function is a **closed-form transform** on its inputs.
No randomness. No I/O. No timestamps. Same inputs → identical scores.

The public entry point is :func:`score_investigation` which returns a
``ScoreCard`` — a frozen dataclass containing every dimension score
and the weighted overall.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from sentinel_core.models._coerce import coerce_seq
from tests.synthetic.schemas import Scenario


# ---------------------------------------------------------------------------
# Scoring weights (sum to 1.0)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "root_cause_match":         0.30,
    "evidence_completeness":    0.15,
    "red_herring_resistance":   0.10,
    "confidence_calibration":   0.10,
    "decision_trace_quality":   0.15,
    "runtime_cost_score":       0.10,
    "mtti_score":               0.10,
}


# ---------------------------------------------------------------------------
# ScoreCard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreCard:
    # RC-L: dimension scores are ``float | None``. ``None`` signals
    # NOT MEASURED (scenario has no ground truth for that dimension)
    # — the aggregator excludes such dimensions from ``overall_score``
    # and renormalises the remaining weights. A ``0.0`` score is a
    # measurement of "fully wrong", distinctly reported.
    scenario_id:             str
    root_cause_match:        float | None = 0.0
    evidence_completeness:   float | None = 0.0
    red_herring_resistance:  float | None = 0.0
    confidence_calibration:  float | None = 0.0
    decision_trace_quality:  float | None = 0.0
    runtime_cost_score:      float | None = 0.0
    mtti_score:              float | None = 0.0
    overall_score:           float = 0.0
    weights:                 Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS)
    )
    notes:                   tuple[str, ...] = ()
    # RC-L: names of dimensions that were NOT MEASURED (returned ``None``
    # from their scorer). Additive — existing readers ignore the field;
    # new readers can distinguish "measured 0.0" from "not measured".
    not_measured:            tuple[str, ...] = ()
    schema_version:          int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["weights"] = dict(d["weights"])
        d["notes"]   = list(d["notes"])
        d["not_measured"] = list(d["not_measured"])
        return d


# ---------------------------------------------------------------------------
# Individual dimensions
# ---------------------------------------------------------------------------

def _tokens(s: str) -> set[str]:
    """Case-folded token set, stripped of common punctuation."""
    if not s:
        return set()
    raw = s.lower()
    out: set[str] = set()
    for tok in raw.split():
        t = tok.strip(".,;:()[]!?\"'`<>-")
        if len(t) >= 3:
            out.add(t)
    return out


def score_root_cause_match(expected: str, reported: str) -> float:
    """Token-overlap Jaccard between expected and reported RCA sentences.

    Deterministic. Returns 0.0 when either string is empty.
    """
    a = _tokens(expected)
    b = _tokens(reported)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def score_evidence_completeness(
    required: tuple[str, ...] | list[str],
    reported_keys: tuple[str, ...] | list[str],
) -> float | None:
    """Fraction of required evidence keys present in the reported set.

    RC-H: inputs coerced via :func:`coerce_seq` so a string is not
    iterated as characters.

    RC-L: **returns ``None`` when ``required`` is empty** — the scenario
    provides no ground truth for this dimension, so it is NOT MEASURED.
    Previously the function returned ``1.0``, silently inflating the
    benchmark's overall score. The aggregator (:func:`score_investigation`)
    treats ``None`` as skip-this-dimension and renormalises the
    remaining weights.
    """
    req = tuple(str(x) for x in coerce_seq(required))
    got = set(str(x) for x in coerce_seq(reported_keys))
    if not req:
        return None  # RC-L: NOT MEASURED
    hits = sum(1 for r in req if r in got)
    return round(hits / len(req), 4)


def score_red_herring_resistance(
    red_herrings: tuple[str, ...] | list[str],
    reported_root_cause: str,
) -> float:
    """1 − (fraction of red-herring keywords that appear in the reported RCA)."""
    rh = tuple(str(x).lower() for x in (red_herrings or ()))
    if not rh:
        return 1.0
    rc = (reported_root_cause or "").lower()
    hits = sum(1 for h in rh if h and h in rc)
    return round(max(0.0, 1.0 - hits / len(rh)), 4)


def score_confidence_calibration(
    expected_range: tuple[int, int] | list[int],
    reported: int,
) -> float:
    """1.0 if reported ∈ [lo, hi]; else linear falloff by 1/50 per point outside."""
    if not isinstance(expected_range, (list, tuple)) or len(expected_range) != 2:
        return 0.0
    try:
        lo = int(expected_range[0])
        hi = int(expected_range[1])
        r  = int(reported)
    except (TypeError, ValueError):
        return 0.0
    if lo <= r <= hi:
        return 1.0
    if r < lo:
        d = lo - r
    else:
        d = r - hi
    return round(max(0.0, 1.0 - d / 50.0), 4)


def score_decision_trace_quality(
    expected_signals: tuple[str, ...] | list[str],
    reported_signals: tuple[str, ...] | list[str],
) -> float | None:
    """Fraction of expected decision signals present in the reported set.

    RC-H: ``coerce_seq`` applied to both inputs.

    RC-L: **returns ``None`` when ``expected_signals`` is empty** —
    NOT MEASURED. Previously returned ``1.0``, inflating overall.
    """
    exp = tuple(str(x) for x in coerce_seq(expected_signals))
    got = set(str(x) for x in coerce_seq(reported_signals))
    if not exp:
        return None  # RC-L: NOT MEASURED
    hits = sum(1 for e in exp if e in got)
    return round(hits / len(exp), 4)


def score_runtime_cost(budget: int, actual: int) -> float:
    """1.0 if actual ≤ budget; else linear falloff by 1/budget per unit over."""
    if budget <= 0:
        return 1.0 if actual == 0 else 0.0
    try:
        b = int(budget)
        a = int(actual)
    except (TypeError, ValueError):
        return 0.0
    if a <= b:
        return 1.0
    over = a - b
    return round(max(0.0, 1.0 - over / b), 4)


def score_mtti(budget_ms: int, actual_ms: int) -> float:
    """1.0 if actual ≤ budget; else linear falloff by 1/budget per ms over."""
    if budget_ms <= 0:
        return 1.0 if actual_ms == 0 else 0.0
    try:
        b = int(budget_ms)
        a = int(actual_ms)
    except (TypeError, ValueError):
        return 0.0
    if a <= b:
        return 1.0
    over = a - b
    return round(max(0.0, 1.0 - over / b), 4)


# ---------------------------------------------------------------------------
# Overall scoring
# ---------------------------------------------------------------------------

def score_investigation(
    scenario: Scenario,
    investigation_output: Mapping[str, Any] | None = None,
    weights: Mapping[str, float] | None = None,
) -> ScoreCard:
    """Score an investigation output against a scenario.

    If ``investigation_output`` is None, falls back to
    ``scenario.mock_investigation_output``. Missing fields degrade
    gracefully: missing → default → per-dimension 0.
    """
    io: Mapping[str, Any] = (
        investigation_output
        if investigation_output is not None
        else scenario.mock_investigation_output
    ) or {}

    root_cause_match = score_root_cause_match(
        scenario.expected_root_cause,
        str(io.get("root_cause", "")),
    )
    evidence_completeness = score_evidence_completeness(
        scenario.required_evidence,
        io.get("evidence_keys", []) or [],
    )
    red_herring_resistance = score_red_herring_resistance(
        scenario.red_herrings,
        str(io.get("root_cause", "")),
    )
    confidence_calibration = score_confidence_calibration(
        scenario.expected_confidence_range,
        int(io.get("confidence", 0) or 0),
    )
    decision_trace_quality = score_decision_trace_quality(
        scenario.expected_decision_signals,
        io.get("decision_signals", []) or [],
    )
    runtime_cost_score = score_runtime_cost(
        scenario.expected_runtime_cost_budget,
        int(io.get("runtime_cost", 0) or 0),
    )
    mtti_score = score_mtti(
        scenario.expected_mtti_budget_ms,
        int(io.get("mtti_ms", 0) or 0),
    )

    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    # RC-L: exclude NOT-MEASURED dimensions from ``overall_score`` and
    # renormalise the remaining weights so the aggregate reflects only
    # what was actually measured. If every dimension is None (an
    # extreme edge case), ``overall_score`` is 0.0 — the report can be
    # inspected via ``not_measured``.
    per_dim: list[tuple[str, float | None]] = [
        ("root_cause_match",        root_cause_match),
        ("evidence_completeness",   evidence_completeness),
        ("red_herring_resistance",  red_herring_resistance),
        ("confidence_calibration",  confidence_calibration),
        ("decision_trace_quality",  decision_trace_quality),
        ("runtime_cost_score",      runtime_cost_score),
        ("mtti_score",              mtti_score),
    ]
    measured = [(name, score) for name, score in per_dim if score is not None]
    not_measured = tuple(sorted(name for name, score in per_dim if score is None))

    total_measured_weight = sum(w[name] for name, _ in measured)
    if total_measured_weight > 0:
        overall = sum(w[name] * score for name, score in measured) / total_measured_weight
    else:
        overall = 0.0

    notes: list[str] = []
    if investigation_output is None:
        notes.append("scored_against_mock_output")
    if not_measured:
        # Deterministic, human-readable audit trail on the ScoreCard.
        notes.append(
            "not_measured_dimensions=" + ",".join(not_measured)
        )

    return ScoreCard(
        scenario_id=scenario.scenario_id,
        root_cause_match=root_cause_match,
        evidence_completeness=evidence_completeness,
        red_herring_resistance=red_herring_resistance,
        confidence_calibration=confidence_calibration,
        decision_trace_quality=decision_trace_quality,
        runtime_cost_score=runtime_cost_score,
        mtti_score=mtti_score,
        overall_score=round(overall, 4),
        weights=w,
        notes=tuple(notes),
        not_measured=not_measured,
    )


__all__ = [
    "DEFAULT_WEIGHTS",
    "ScoreCard",
    "score_root_cause_match",
    "score_evidence_completeness",
    "score_red_herring_resistance",
    "score_confidence_calibration",
    "score_decision_trace_quality",
    "score_runtime_cost",
    "score_mtti",
    "score_investigation",
]
