"""RCAPathRanker — rank symptom→RCA paths by confidence + recurrence."""
from __future__ import annotations

from statistics import mean
from typing import Iterable

from sentinel_core.causal_graph.schemas import RCAPath, make_path_id
from sentinel_core.intel_memory import MemoryRecord


class RCAPathRanker:
    """Compute one :class:`RCAPath` per (service, symptom, root_cause)
    triplet and rank deterministically."""

    def build(self, records: Iterable[MemoryRecord]) -> tuple[RCAPath, ...]:
        buckets: dict[tuple[str, str, str], list[MemoryRecord]] = {}
        for r in records or ():
            sym = r.incident_type or ""
            rc = (r.detected_root_cause or "")[:120].strip().lower()
            if not sym or not rc:
                continue
            buckets.setdefault((r.service or "", sym, rc), []).append(r)

        out: list[RCAPath] = []
        for (svc, sym, rc), group in buckets.items():
            evidence = tuple(sorted({e for r in group for e in r.evidence_collected}))
            avg_conf = mean(int(r.confidence or 0) for r in group) / 100.0 \
                if group else 0.0
            out.append(RCAPath(
                path_id=make_path_id("rca", svc, sym, rc),
                service=svc,
                symptom=sym,
                root_cause=rc,
                evidence_keys=evidence,
                confidence=round(avg_conf, 4),
                recurrence=len(group),
                memory_ids=tuple(sorted(r.memory_id for r in group)),
            ))
        # Rank: recurrence DESC, confidence DESC, path_id
        return tuple(sorted(out, key=lambda p: (-p.recurrence, -p.confidence, p.path_id)))


__all__ = ["RCAPathRanker"]
