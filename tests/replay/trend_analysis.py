"""Trend analysis for SentinelReplay.

Every function is a closed-form transform on a list of numeric values
or a list of :class:`BenchmarkRun`. No randomness, no timestamps.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Mapping

from tests.replay.schemas import BenchmarkRun


# Dimensions the replay engine tracks by name (matches ScoreCard fields).
_DIMENSIONS: tuple[str, ...] = (
    "root_cause_match",
    "evidence_completeness",
    "red_herring_resistance",
    "confidence_calibration",
    "decision_trace_quality",
    "runtime_cost_score",
    "mtti_score",
    "overall_score",
)


# ---------------------------------------------------------------------------
# Univariate trend
# ---------------------------------------------------------------------------

def compute_trend(values: list[float] | tuple[float, ...]) -> dict[str, Any]:
    """Compute a compact deterministic trend descriptor.

    Returns:
        {mean, min, max, first, last, slope, direction, count}
      where ``slope = (last - first) / (n - 1)`` for n ≥ 2 else 0.0
      and ``direction`` ∈ {"up", "down", "stable"} using a small dead-band.
    """
    v = [float(x) for x in (values or ())]
    if not v:
        return {"mean": 0.0, "min": 0.0, "max": 0.0, "first": 0.0,
                 "last": 0.0, "slope": 0.0, "direction": "stable", "count": 0}
    m = round(mean(v), 4)
    lo = round(min(v), 4)
    hi = round(max(v), 4)
    first = round(v[0], 4)
    last = round(v[-1], 4)
    if len(v) < 2:
        slope = 0.0
    else:
        slope = round((last - first) / (len(v) - 1), 4)
    if slope > 0.01:
        direction = "up"
    elif slope < -0.01:
        direction = "down"
    else:
        direction = "stable"
    return {
        "mean":      m,
        "min":       lo,
        "max":       hi,
        "first":     first,
        "last":      last,
        "slope":     slope,
        "direction": direction,
        "count":     len(v),
    }


# ---------------------------------------------------------------------------
# Aggregated trends over runs
# ---------------------------------------------------------------------------

def _dimension_series(runs: tuple[BenchmarkRun, ...],
                       dimension: str) -> list[float]:
    """Per-run mean of a given dimension across scorecards."""
    out: list[float] = []
    for r in runs:
        if not r.scorecards:
            out.append(0.0)
            continue
        out.append(mean(getattr(c, dimension) for c in r.scorecards))
    return out


def compute_dimension_trend(runs: tuple[BenchmarkRun, ...],
                              dimension: str) -> dict[str, Any]:
    if dimension not in _DIMENSIONS:
        raise ValueError(f"unknown dimension: {dimension}")
    return compute_trend(_dimension_series(runs, dimension))


def compute_all_trends(runs: tuple[BenchmarkRun, ...]) -> dict[str, dict[str, Any]]:
    """Trend descriptor for every dimension. Deterministic ordering."""
    return {d: compute_dimension_trend(runs, d) for d in _DIMENSIONS}


# ---------------------------------------------------------------------------
# Timeline + regression detection
# ---------------------------------------------------------------------------

def build_timeline(runs: tuple[BenchmarkRun, ...]) -> list[dict[str, Any]]:
    """One row per run — overall mean + per-dimension means.

    Rows are sorted by ``generated_at`` ascending, then by ``run_id``
    for stable ordering when timestamps tie or are empty.
    """
    sorted_runs = sorted(runs, key=lambda r: (r.generated_at, r.run_id))
    rows: list[dict[str, Any]] = []
    for r in sorted_runs:
        row: dict[str, Any] = {
            "run_id":         r.run_id,
            "generated_at":   r.generated_at,
            "scorecard_count": len(r.scorecards),
        }
        for d in _DIMENSIONS:
            if r.scorecards:
                row[d] = round(mean(getattr(c, d) for c in r.scorecards), 4)
            else:
                row[d] = 0.0
        rows.append(row)
    return rows


def detect_regressions(runs: tuple[BenchmarkRun, ...],
                        threshold: float = 0.05) -> list[dict[str, Any]]:
    """Return per-run-transition regressions where any dimension dropped
    by more than ``threshold`` between adjacent runs (sorted by
    generated_at)."""
    timeline = build_timeline(runs)
    out: list[dict[str, Any]] = []
    for i in range(1, len(timeline)):
        prev = timeline[i - 1]
        curr = timeline[i]
        for d in _DIMENSIONS:
            delta = round(curr[d] - prev[d], 4)
            if delta < -threshold:
                out.append({
                    "from_run":  prev["run_id"],
                    "to_run":    curr["run_id"],
                    "dimension": d,
                    "delta":     delta,
                    "from_value": prev[d],
                    "to_value":  curr[d],
                })
    return out


def detect_improvements(runs: tuple[BenchmarkRun, ...],
                         threshold: float = 0.05) -> list[dict[str, Any]]:
    """Return per-run-transition improvements where any dimension rose
    by more than ``threshold`` between adjacent runs."""
    timeline = build_timeline(runs)
    out: list[dict[str, Any]] = []
    for i in range(1, len(timeline)):
        prev = timeline[i - 1]
        curr = timeline[i]
        for d in _DIMENSIONS:
            delta = round(curr[d] - prev[d], 4)
            if delta > threshold:
                out.append({
                    "from_run":  prev["run_id"],
                    "to_run":    curr["run_id"],
                    "dimension": d,
                    "delta":     delta,
                    "from_value": prev[d],
                    "to_value":  curr[d],
                })
    return out


__all__ = [
    "compute_trend",
    "compute_dimension_trend",
    "compute_all_trends",
    "build_timeline",
    "detect_regressions",
    "detect_improvements",
]
