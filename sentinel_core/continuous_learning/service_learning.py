"""Service-level reliability profile."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any, Iterable

from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class ServiceLearningRow:
    service:              str
    incident_count:       int
    average_mtti_ms:      int
    average_confidence:   int
    success_rate:         float
    top_root_causes:      tuple[tuple[str, int], ...] = ()
    schema_version:       int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["success_rate"] = round(float(d["success_rate"]), 4)
        d["top_root_causes"] = [
            {"root_cause": rc, "count": n}
            for rc, n in self.top_root_causes
        ]
        return d


class ServiceLearning:
    """Per-service reliability + top root causes."""

    def score(self, records: Iterable[MemoryRecord]) -> tuple[ServiceLearningRow, ...]:
        per_service: dict[str, list[MemoryRecord]] = {}
        for r in records or ():
            if not r.service:
                continue
            per_service.setdefault(r.service, []).append(r)
        rows: list[ServiceLearningRow] = []
        for svc in sorted(per_service.keys()):
            g = per_service[svc]
            avg_mtti = int(mean(int(r.mtti_ms or 0) for r in g)) if g else 0
            avg_conf = int(mean(int(r.confidence or 0) for r in g)) if g else 0
            succ = sum(1 for r in g if float(r.investigation_score or 0.0) >= 0.5)
            rate = succ / len(g) if g else 0.0
            rc_counts: Counter = Counter(
                (r.detected_root_cause or "")[:120].strip()
                for r in g if r.detected_root_cause
            )
            top = tuple(rc_counts.most_common(3))
            rows.append(ServiceLearningRow(
                service=svc,
                incident_count=len(g),
                average_mtti_ms=avg_mtti,
                average_confidence=avg_conf,
                success_rate=round(rate, 4),
                top_root_causes=top,
            ))
        return tuple(sorted(rows,
                              key=lambda r: (-r.incident_count, r.service)))


__all__ = ["ServiceLearningRow", "ServiceLearning"]
