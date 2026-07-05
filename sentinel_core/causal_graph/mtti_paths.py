"""MTTIPathRanker — rank paths by MTTI-reduction potential."""
from __future__ import annotations

from statistics import mean
from typing import Iterable

from sentinel_core.causal_graph.schemas import MTTIPath, make_path_id
from sentinel_core.intel_memory import MemoryRecord


class MTTIPathRanker:
    def build(self, records: Iterable[MemoryRecord]) -> tuple[MTTIPath, ...]:
        buckets: dict[tuple[str, str, tuple[str, ...], str], list[MemoryRecord]] = {}
        for r in records or ():
            svc = r.service or ""
            rc = (r.detected_root_cause or "")[:120].strip().lower()
            ordering = tuple(r.evidence_ordering)
            remediation = (r.resolution or "")[:120].strip().lower()
            if not rc:
                continue
            buckets.setdefault((svc, rc, ordering, remediation), []).append(r)

        out: list[MTTIPath] = []
        for (svc, rc, ordering, rm), group in buckets.items():
            mtti_vals = [int(r.mtti_ms or 0) for r in group
                         if int(r.mtti_ms or 0) > 0]
            avg = int(mean(mtti_vals)) if mtti_vals else 0
            best = min(mtti_vals) if mtti_vals else 0
            out.append(MTTIPath(
                path_id=make_path_id("mtti", svc, rc, "|".join(ordering), rm),
                service=svc,
                root_cause=rc,
                evidence_ordering=ordering,
                remediation=rm,
                average_mtti_ms=avg,
                best_mtti_ms=best,
                memory_ids=tuple(sorted(r.memory_id for r in group)),
            ))
        # Rank by best MTTI ASC (fastest paths first), then average ASC,
        # then path_id for stable ordering.
        return tuple(sorted(out, key=lambda p: (p.best_mtti_ms or 10**12,
                                                    p.average_mtti_ms or 10**12,
                                                    p.path_id)))


__all__ = ["MTTIPathRanker"]
