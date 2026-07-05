"""Strategy feedback aggregation."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Iterable

from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class StrategyFeedbackRow:
    capability_id:       str
    total_uses:          int
    successful_uses:     int
    effectiveness:       float
    average_mtti_ms:     int
    schema_version:      int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["effectiveness"] = round(float(d["effectiveness"]), 4)
        return d


class StrategyFeedback:
    """Effectiveness per capability id — same shape as Strategy Optimizer
    output but expressed as a feedback signal (append-only)."""

    def score(self, records: Iterable[MemoryRecord]) -> tuple[StrategyFeedbackRow, ...]:
        totals: Counter = Counter()
        succ:   Counter = Counter()
        mtti:   dict[str, list[int]] = {}
        for r in records or ():
            success = float(r.investigation_score or 0.0) >= 0.5
            for c in r.planner_decisions:
                totals[c] += 1
                if success:
                    succ[c] += 1
                mtti.setdefault(c, []).append(int(r.mtti_ms or 0))
        rows = []
        for c in sorted(totals.keys()):
            total = totals[c]
            s = succ.get(c, 0)
            eff = s / total if total else 0.0
            rows.append(StrategyFeedbackRow(
                capability_id=c,
                total_uses=total,
                successful_uses=s,
                effectiveness=round(eff, 4),
                average_mtti_ms=int(mean(mtti[c])) if mtti[c] else 0,
            ))
        return tuple(sorted(rows,
                              key=lambda r: (-r.effectiveness, -r.total_uses,
                                              r.capability_id)))


__all__ = ["StrategyFeedbackRow", "StrategyFeedback"]
