"""Deterministic ranker for MemoryRecord similarity results.

Wraps SimilarityEngine.score_many with top-N cap and stable sort.
"""
from __future__ import annotations

from sentinel_core.intel_memory.schemas import MemoryRecord, SimilarityScore
from sentinel_core.intel_memory.similarity import SimilarityEngine


class Ranker:
    """Rank candidate MemoryRecords against a query."""

    def __init__(self, engine: SimilarityEngine | None = None) -> None:
        self._engine = engine or SimilarityEngine()

    def top_n(
        self,
        query: MemoryRecord,
        candidates: tuple[MemoryRecord, ...],
        n: int = 10,
    ) -> tuple[SimilarityScore, ...]:
        n = max(0, int(n))
        scored = self._engine.score_many(query, candidates)
        return scored[:n]


__all__ = [
    "Ranker",
]
