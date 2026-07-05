"""Heatmap builder — deterministic scenario × dimension score matrix.

Produces a JSON-safe 2D representation of a benchmark run's scores.
Useful for visualisation callers.
"""
from __future__ import annotations

from typing import Any

from tests.replay.schemas import BenchmarkRun


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


def build_heatmap(run: BenchmarkRun) -> dict[str, Any]:
    """One heatmap per run. Rows are scenarios (sorted), cols are
    dimensions (fixed order)."""
    if run is None or not run.scorecards:
        return {
            "run_id":       getattr(run, "run_id", ""),
            "rows":         [],
            "cols":         list(_DIMENSIONS),
            "values":       [],
        }
    cards = sorted(run.scorecards, key=lambda c: c.scenario_id)
    values: list[list[float]] = []
    rows: list[str] = []
    for c in cards:
        rows.append(c.scenario_id)
        values.append([round(float(getattr(c, d)), 4) for d in _DIMENSIONS])
    return {
        "run_id": run.run_id,
        "rows":   rows,
        "cols":   list(_DIMENSIONS),
        "values": values,
    }


def build_heatmap_series(runs: tuple[BenchmarkRun, ...]) -> list[dict[str, Any]]:
    """One heatmap per run, sorted by generated_at then run_id."""
    sorted_runs = sorted(runs, key=lambda r: (r.generated_at, r.run_id))
    return [build_heatmap(r) for r in sorted_runs]


__all__ = [
    "build_heatmap",
    "build_heatmap_series",
]
