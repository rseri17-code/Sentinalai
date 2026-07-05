"""RecurrenceDetector — recurrence groupings across dimensions."""
from __future__ import annotations

from statistics import mean
from typing import Iterable

from sentinel_core.causal_graph.schemas import CausalRecurrence
from sentinel_core.intel_memory import MemoryRecord


class RecurrenceDetector:
    def __init__(self, *, min_count: int = 2) -> None:
        self._min = max(1, int(min_count))

    def by_root_cause(self, records: Iterable[MemoryRecord]) -> tuple[CausalRecurrence, ...]:
        return self._group(records, kind="root_cause",
                             sig=lambda r: (r.detected_root_cause or "")[:120].strip().lower())

    def by_service(self, records: Iterable[MemoryRecord]) -> tuple[CausalRecurrence, ...]:
        return self._group(records, kind="service", sig=lambda r: r.service or "")

    def by_symptom(self, records: Iterable[MemoryRecord]) -> tuple[CausalRecurrence, ...]:
        return self._group(records, kind="symptom", sig=lambda r: r.incident_type or "")

    def by_evidence_pattern(self, records: Iterable[MemoryRecord]) -> tuple[CausalRecurrence, ...]:
        return self._group(records, kind="evidence_pattern",
                             sig=lambda r: ",".join(sorted(r.evidence_collected)))

    def by_remediation(self, records: Iterable[MemoryRecord]) -> tuple[CausalRecurrence, ...]:
        return self._group(records, kind="remediation",
                             sig=lambda r: (r.resolution or "")[:120].strip().lower())

    def all_recurrences(self, records: Iterable[MemoryRecord]) -> tuple[CausalRecurrence, ...]:
        records = tuple(records or ())
        combined = (
            self.by_root_cause(records)
            + self.by_service(records)
            + self.by_symptom(records)
            + self.by_evidence_pattern(records)
            + self.by_remediation(records)
        )
        return tuple(sorted(combined, key=lambda r: (r.kind, -r.count, r.signature)))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _group(
        self, records: Iterable[MemoryRecord], *, kind: str, sig,
    ) -> tuple[CausalRecurrence, ...]:
        buckets: dict[str, list[MemoryRecord]] = {}
        for r in records or ():
            s = sig(r)
            if not s:
                continue
            buckets.setdefault(s, []).append(r)
        out: list[CausalRecurrence] = []
        for s, group in buckets.items():
            if len(group) < self._min:
                continue
            out.append(CausalRecurrence(
                signature=s,
                kind=kind,
                count=len(group),
                memory_ids=tuple(sorted(r.memory_id for r in group)),
                average_confidence=int(mean(int(r.confidence or 0) for r in group))
                    if group else 0,
                average_mtti_ms=int(mean(int(r.mtti_ms or 0) for r in group))
                    if group else 0,
            ))
        return tuple(sorted(out, key=lambda p: (-p.count, p.signature)))


__all__ = ["RecurrenceDetector"]
