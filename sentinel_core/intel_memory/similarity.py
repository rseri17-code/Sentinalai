"""Deterministic similarity engine for MemoryRecords.

Given a *query* MemoryRecord and a candidate MemoryRecord, compute an
11-dimension weighted similarity score in `[0.0, 1.0]`. No embeddings,
no LLM, no external services.
"""
from __future__ import annotations

from typing import Any, Mapping

from sentinel_core.intel_memory.schemas import MemoryRecord, SimilarityScore


# ---------------------------------------------------------------------------
# Dimension weights (sum = 1.0)
# ---------------------------------------------------------------------------

SIMILARITY_WEIGHTS: Mapping[str, float] = {
    "exact":         0.15,
    "topology":      0.12,
    "dependency":    0.08,
    "evidence":      0.15,
    "planner":       0.10,
    "transaction":   0.08,
    "root_cause":    0.15,
    "infrastructure": 0.05,
    "resolution":    0.05,
    "blast_radius":  0.07,
    # 11th dimension reserved for consistency (near_match placeholder)
}
# NOTE: 11 dimensions total; keep sum ≤ 1.0. Remaining margin (~0%) is
# reserved for future near-match refinement.


# ---------------------------------------------------------------------------
# Similarity primitives
# ---------------------------------------------------------------------------

def _tokens(s: str) -> set[str]:
    if not s:
        return set()
    out: set[str] = set()
    for tok in s.lower().split():
        t = tok.strip(".,;:()[]!?\"'`<>-")
        if len(t) >= 3:
            out.add(t)
    return out


def _jaccard(a: set[Any], b: set[Any]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _string_eq(a: str, b: str) -> float:
    a_norm = (a or "").strip().lower()
    b_norm = (b or "").strip().lower()
    if not a_norm or not b_norm:
        return 0.0
    return 1.0 if a_norm == b_norm else 0.0


def _topology_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    # Weighted union of service, namespace, cluster, region overlaps
    subscores = [
        _jaccard(set(a.topology.services),   set(b.topology.services)),
        _jaccard(set(a.topology.namespaces), set(b.topology.namespaces)),
        _jaccard(set(a.topology.clusters),   set(b.topology.clusters)),
        _jaccard(set(a.topology.regions),    set(b.topology.regions)),
    ]
    return sum(subscores) / len(subscores)


def _dependency_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    return _jaccard(set(a.topology.dependencies),
                     set(b.topology.dependencies))


def _evidence_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    return _jaccard(set(a.evidence_collected), set(b.evidence_collected))


def _planner_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    # Ordering matters — use LCS-lite: shared-prefix ratio + Jaccard blend
    p1, p2 = a.planner_decisions, b.planner_decisions
    jac = _jaccard(set(p1), set(p2))
    if not p1 or not p2:
        prefix = 0.0
    else:
        common = 0
        for x, y in zip(p1, p2):
            if x == y:
                common += 1
            else:
                break
        prefix = common / max(len(p1), len(p2))
    return round(0.5 * jac + 0.5 * prefix, 4)


def _transaction_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    return _jaccard(set(a.transaction_path), set(b.transaction_path))


def _root_cause_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    return _jaccard(_tokens(a.detected_root_cause),
                     _tokens(b.detected_root_cause))


def _infrastructure_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    subscores = [
        _string_eq(a.topology.cloud,   b.topology.cloud),
        _string_eq(a.topology.gateway, b.topology.gateway),
        _string_eq(a.topology.idp,     b.topology.idp),
        _string_eq(a.topology.dns,     b.topology.dns),
        _jaccard(set(a.topology.databases), set(b.topology.databases)),
    ]
    return sum(subscores) / len(subscores)


def _resolution_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    return _jaccard(_tokens(a.resolution), _tokens(b.resolution))


def _blast_similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    subscores = [
        _string_eq(a.blast_radius.severity, b.blast_radius.severity),
        _jaccard(set(a.blast_radius.affected), set(b.blast_radius.affected)),
    ]
    return sum(subscores) / len(subscores)


# ---------------------------------------------------------------------------
# SimilarityEngine
# ---------------------------------------------------------------------------

class SimilarityEngine:
    """Deterministic weighted similarity comparator.

    :attr:`weights` is a mapping from dimension name to weight. Custom
    weights may be passed; missing keys inherit :data:`SIMILARITY_WEIGHTS`.
    """

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        self._weights = dict(SIMILARITY_WEIGHTS)
        if weights:
            for k, v in weights.items():
                self._weights[k] = float(v)

    def score(self, query: MemoryRecord, candidate: MemoryRecord) -> SimilarityScore:
        breakdown = {
            "topology":       _topology_similarity(query, candidate),
            "dependency":     _dependency_similarity(query, candidate),
            "evidence":       _evidence_similarity(query, candidate),
            "planner":        _planner_similarity(query, candidate),
            "transaction":    _transaction_similarity(query, candidate),
            "root_cause":     _root_cause_similarity(query, candidate),
            "infrastructure": _infrastructure_similarity(query, candidate),
            "resolution":     _resolution_similarity(query, candidate),
            "blast_radius":   _blast_similarity(query, candidate),
        }
        exact = bool(query.fingerprint) and query.fingerprint == candidate.fingerprint
        breakdown["exact"] = 1.0 if exact else 0.0

        overall = 0.0
        for k, v in breakdown.items():
            overall += self._weights.get(k, 0.0) * v
        # Add a small explicit reward when service and incident_type both match
        # so semantically identical incidents cluster tightly.
        if query.service and query.service == candidate.service:
            if query.incident_type and query.incident_type == candidate.incident_type:
                overall = min(1.0, overall + 0.02)

        return SimilarityScore(
            memory_id=candidate.memory_id,
            overall=round(overall, 4),
            breakdown={k: round(v, 4) for k, v in breakdown.items()},
            exact_match=exact,
        )

    def score_many(
        self, query: MemoryRecord, candidates: tuple[MemoryRecord, ...]
    ) -> tuple[SimilarityScore, ...]:
        scores = tuple(
            self.score(query, c) for c in candidates
            if c.memory_id != query.memory_id
        )
        # Deterministic: highest overall first; tie-break by memory_id ASC
        return tuple(sorted(scores, key=lambda s: (-s.overall, s.memory_id)))


__all__ = [
    "SIMILARITY_WEIGHTS",
    "SimilarityEngine",
]
