"""Confidence calibration — bin predictions vs actual outcomes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Iterable

from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class CalibrationBin:
    predicted_lo:        int
    predicted_hi:        int
    predicted_count:     int
    average_predicted:   int
    actual_success_rate: float
    memory_ids:          tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["actual_success_rate"] = round(float(d["actual_success_rate"]), 4)
        d["memory_ids"] = sorted(d["memory_ids"])
        return d


class ConfidenceCalibrator:
    """Deterministic calibration binning.

    A record is a "success" iff its ``investigation_score`` >= 0.5.
    Bins are 5×20-point ranges: [0,20), [20,40), ..., [80,101).
    """

    def calibrate(
        self, records: Iterable[MemoryRecord],
    ) -> tuple[CalibrationBin, ...]:
        # Guard rails: bin edges are inclusive-exclusive except the last
        # which is inclusive-inclusive (0-100 range).
        edges = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101))
        buckets: dict[tuple[int, int], list[MemoryRecord]] = {e: [] for e in edges}
        for r in records or ():
            # RC-C: clamp to valid confidence range before binning so every
            # record lands in exactly one bin. Previously values >100 or <0
            # fell through every bin and were silently dropped, making
            # sum(predicted_count) < len(records) — an invisible loss.
            c = max(0, min(100, int(r.confidence or 0)))
            for lo, hi in edges:
                if lo <= c < hi:
                    buckets[(lo, hi)].append(r)
                    break
        out: list[CalibrationBin] = []
        for (lo, hi) in edges:
            g = buckets[(lo, hi)]
            if not g:
                out.append(CalibrationBin(
                    predicted_lo=lo, predicted_hi=hi,
                    predicted_count=0, average_predicted=0,
                    actual_success_rate=0.0, memory_ids=(),
                ))
                continue
            avg_pred = int(mean(int(r.confidence or 0) for r in g))
            successes = sum(
                1 for r in g if float(r.investigation_score or 0.0) >= 0.5
            )
            rate = successes / len(g)
            out.append(CalibrationBin(
                predicted_lo=lo, predicted_hi=hi,
                predicted_count=len(g),
                average_predicted=avg_pred,
                actual_success_rate=round(rate, 4),
                memory_ids=tuple(sorted(r.memory_id for r in g)),
            ))
        return tuple(out)


__all__ = ["CalibrationBin", "ConfidenceCalibrator"]
