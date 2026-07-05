"""Learning engine — identify repeated weaknesses across runs.

Every weakness is a closed-form projection over the run corpus. No
mutation of production code; the engine only emits :class:`WeaknessRecord`
instances.
"""
from __future__ import annotations

from statistics import mean
from typing import Any

from tests.replay.schemas import (
    BenchmarkRun,
    WeaknessRecord,
    WeaknessType,
)


# Threshold below which a dimension score is considered "weak".
_DEFAULT_WEAK_THRESHOLD = 0.6

# Minimum number of consecutive weak runs before a weakness is recorded.
_DEFAULT_MIN_CONSECUTIVE = 2


# Dimension → WeaknessType mapping. Every weakness observed on a
# dimension deterministically maps to one weakness type.
_DIM_TO_WEAKNESS: dict[str, str] = {
    "root_cause_match":        WeaknessType.INCORRECT_ROOT_CAUSE.value,
    "evidence_completeness":   WeaknessType.MISSING_EVIDENCE.value,
    "red_herring_resistance":  WeaknessType.FALSE_POSITIVE.value,
    "confidence_calibration":  WeaknessType.LOW_CONFIDENCE.value,
    "decision_trace_quality":  WeaknessType.PLANNER_MISTAKE.value,
    "runtime_cost_score":      WeaknessType.RECURRING_INCIDENT_CLASS.value,
    "mtti_score":              WeaknessType.RECURRING_INCIDENT_CLASS.value,
}


class LearningEngine:
    """Deterministic repeat-weakness detector.

    A weakness is emitted when a given (scenario_id, dimension) pair
    has been at or below ``weak_threshold`` for at least
    ``min_consecutive`` of the most-recent runs.
    """

    def __init__(
        self,
        *,
        weak_threshold: float = _DEFAULT_WEAK_THRESHOLD,
        min_consecutive: int = _DEFAULT_MIN_CONSECUTIVE,
    ) -> None:
        self._threshold = float(weak_threshold)
        self._min = max(1, int(min_consecutive))

    def analyze(
        self, runs: tuple[BenchmarkRun, ...]
    ) -> tuple[WeaknessRecord, ...]:
        """Scan ``runs`` (assumed sorted oldest → newest by caller) and
        return every repeated weakness. Deterministic ordering."""
        if not runs:
            return ()

        # Preserve stable ordering (sorted by generated_at, then run_id)
        sorted_runs = sorted(runs, key=lambda r: (r.generated_at, r.run_id))

        # Collect per (scenario_id, dimension) time series
        # {(scenario_id, dim): [(run_id, score), ...]}
        series: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for r in sorted_runs:
            for card in r.scorecards:
                for dim in _DIM_TO_WEAKNESS.keys():
                    key = (card.scenario_id, dim)
                    series.setdefault(key, []).append(
                        (r.run_id, float(getattr(card, dim)))
                    )

        records: list[WeaknessRecord] = []
        for (scenario_id, dim), points in series.items():
            # Count the tail-most consecutive weak values
            tail_weak_scores = []
            for _run_id, score in reversed(points):
                if score <= self._threshold:
                    tail_weak_scores.append(score)
                else:
                    break
            if len(tail_weak_scores) >= self._min:
                avg = mean(tail_weak_scores)
                records.append(WeaknessRecord(
                    weakness_type=_DIM_TO_WEAKNESS[dim],
                    scenario_id=scenario_id,
                    dimension=dim,
                    count=len(tail_weak_scores),
                    average_score=round(avg, 4),
                ))

        # Deterministic sort: highest count first; ties break by
        # scenario_id ASC, dimension ASC.
        records.sort(key=lambda w: (-w.count, w.scenario_id, w.dimension))
        return tuple(records)

    def leaderboard(
        self,
        weaknesses: tuple[WeaknessRecord, ...] | list[WeaknessRecord],
        limit: int = 10,
    ) -> tuple[WeaknessRecord, ...]:
        """Top-N weaknesses ranked by (count DESC, average_score ASC)."""
        ordered = sorted(
            weaknesses,
            key=lambda w: (-w.count, w.average_score, w.scenario_id, w.dimension),
        )
        return tuple(ordered[: max(0, int(limit))])


__all__ = [
    "LearningEngine",
]
