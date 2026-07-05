"""Hypothesis learning across corpus.

Hypotheses live on ``MemoryRecord.decision_trace.hypotheses`` when the
Hypothesis Intelligence library populated them. Any missing / bad
input degrades cleanly.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class HypothesisAccuracyRow:
    hypothesis:      str
    times_seen:      int
    times_confirmed: int
    times_ruled_out: int
    accuracy:        float
    schema_version:  int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["accuracy"] = round(float(d["accuracy"]), 4)
        return d


class HypothesisFeedback:
    """Aggregate hypothesis confirmation rates across the corpus."""

    def score(self, records: Iterable[MemoryRecord]) -> tuple[HypothesisAccuracyRow, ...]:
        seen: Counter = Counter()
        confirmed: Counter = Counter()
        ruled_out: Counter = Counter()
        for r in records or ():
            hyps = (r.decision_trace or {}).get("hypotheses", [])
            if not isinstance(hyps, list):
                continue
            for h in hyps:
                if isinstance(h, dict):
                    name = str(h.get("name") or h.get("hypothesis") or "")
                    status = str(h.get("status") or "")
                elif isinstance(h, str):
                    name, status = h, ""
                else:
                    continue
                if not name:
                    continue
                seen[name] += 1
                if status == "confirmed":
                    confirmed[name] += 1
                elif status == "ruled_out":
                    ruled_out[name] += 1
        rows: list[HypothesisAccuracyRow] = []
        for name in sorted(seen.keys()):
            n = seen[name]
            c = confirmed.get(name, 0)
            acc = c / n if n else 0.0
            rows.append(HypothesisAccuracyRow(
                hypothesis=name,
                times_seen=n,
                times_confirmed=c,
                times_ruled_out=ruled_out.get(name, 0),
                accuracy=round(acc, 4),
            ))
        return tuple(sorted(rows,
                              key=lambda r: (-r.accuracy, -r.times_seen,
                                              r.hypothesis)))


__all__ = ["HypothesisAccuracyRow", "HypothesisFeedback"]
