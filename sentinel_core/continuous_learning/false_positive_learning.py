"""False-positive tracking across corpus + feedback signals."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from sentinel_core.continuous_learning.feedback_collector import (
    FeedbackCollector,
    FeedbackKind,
    FeedbackSignal,
)
from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class FalsePositiveRow:
    lead:            str
    count:           int
    memory_ids:      tuple[str, ...] = ()
    services:        tuple[str, ...] = ()
    schema_version:  int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["memory_ids"] = sorted(d["memory_ids"])
        d["services"]   = sorted(d["services"])
        return d


class FalsePositiveLearning:
    """Deterministic false-positive aggregator across ``false_leads`` and
    :class:`FeedbackSignal` records."""

    def score(
        self,
        records: Iterable[MemoryRecord],
        feedback: FeedbackCollector | None = None,
    ) -> tuple[FalsePositiveRow, ...]:
        counts: Counter = Counter()
        per_lead_records: dict[str, list[MemoryRecord]] = {}
        for r in records or ():
            for fl in r.false_leads:
                counts[str(fl).lower()] += 1
                per_lead_records.setdefault(str(fl).lower(), []).append(r)
        # Extra count from operator FALSE_POSITIVE feedback signals
        if feedback:
            for sig in feedback.by_kind(FeedbackKind.FALSE_POSITIVE.value):
                key = str(sig.value or "").lower() or f"memory:{sig.memory_id}"
                counts[key] += 1
        rows = []
        for k in sorted(counts.keys()):
            group = per_lead_records.get(k, [])
            rows.append(FalsePositiveRow(
                lead=k,
                count=counts[k],
                memory_ids=tuple(sorted(r.memory_id for r in group)),
                services=tuple(sorted({r.service for r in group if r.service})),
            ))
        return tuple(sorted(rows, key=lambda r: (-r.count, r.lead)))


__all__ = ["FalsePositiveRow", "FalsePositiveLearning"]
