"""Causal chain feedback — reuse cross-incident causal graph."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from sentinel_core.causal_graph import ChainDetector
from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class CausalLearningRow:
    chain_id:            str
    node_ids:            tuple[str, ...]
    recurrences:         int
    average_confidence:  float
    average_mtti_ms:     int
    schema_version:      int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id":           self.chain_id,
            "node_ids":           list(self.node_ids),
            "recurrences":        int(self.recurrences),
            "average_confidence": round(float(self.average_confidence), 4),
            "average_mtti_ms":    int(self.average_mtti_ms),
            "schema_version":     self.schema_version,
        }


class CausalFeedback:
    """Extract deterministic causal-chain feedback from the corpus."""

    def score(self, records: Iterable[MemoryRecord]) -> tuple[CausalLearningRow, ...]:
        chains = ChainDetector(min_count=2).detect(records)
        rows = tuple(CausalLearningRow(
            chain_id=c.chain_id,
            node_ids=c.node_ids,
            recurrences=c.count,
            average_confidence=c.confidence,
            average_mtti_ms=c.average_mtti_ms,
        ) for c in chains)
        return tuple(sorted(rows,
                              key=lambda r: (-r.recurrences,
                                              -r.average_confidence,
                                              r.chain_id)))


__all__ = ["CausalLearningRow", "CausalFeedback"]
