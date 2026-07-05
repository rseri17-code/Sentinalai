"""ChainDetector — extract recurring (symptom → RCA → remediation) chains."""
from __future__ import annotations

from statistics import mean
from typing import Iterable

from sentinel_core.causal_graph.schemas import CausalChain, make_chain_id
from sentinel_core.intel_memory import MemoryRecord


class ChainDetector:
    """Detect recurring causal chains across a MemoryRecord corpus.

    A chain is the tuple (service, failure_mode, root_cause, remediation).
    Recurrence count == how many records share the chain.
    """

    def __init__(self, *, min_count: int = 2) -> None:
        self._min = max(1, int(min_count))

    def detect(self, records: Iterable[MemoryRecord]) -> tuple[CausalChain, ...]:
        buckets: dict[tuple[str, ...], list[MemoryRecord]] = {}
        for r in records or ():
            key = (
                r.service or "",
                r.incident_type or "",
                (r.detected_root_cause or "")[:120].strip().lower(),
                (r.resolution or "")[:120].strip().lower(),
            )
            if not key[2]:              # no root cause → skip
                continue
            buckets.setdefault(key, []).append(r)

        out: list[CausalChain] = []
        for key, group in buckets.items():
            if len(group) < self._min:
                continue
            # Node ids for chain sequence — deterministic strings.
            node_ids = tuple(x for x in key if x)
            chain_id = make_chain_id(node_ids)
            avg_mtti = int(mean(int(r.mtti_ms or 0) for r in group)) \
                if group else 0
            avg_conf = mean(int(r.confidence or 0) for r in group) / 100.0 \
                if group else 0.0
            out.append(CausalChain(
                chain_id=chain_id,
                node_ids=node_ids,
                count=len(group),
                confidence=round(avg_conf, 4),
                average_mtti_ms=avg_mtti,
                memory_ids=tuple(sorted(r.memory_id for r in group)),
            ))
        return tuple(sorted(out, key=lambda c: (-c.count, c.chain_id)))


__all__ = ["ChainDetector"]
