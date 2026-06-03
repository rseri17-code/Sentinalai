"""Hybrid retriever — fuses BM25 (lexical) + cosine TF-IDF (semantic) + source confidence.

Fusion formula:
  raw_score = alpha * bm25_norm + (1 - alpha) * cosine_norm
  final_score = raw_score * source_confidence

where alpha = ALPHA (default 0.6 — BM25 weighted higher for SRE queries
which tend to use specific error codes, service names, and version strings
that BM25 handles better than cosine similarity).

Normalization: min-max per candidate set so scores are comparable across
query types.

Each candidate dict must contain:
  doc_id      — unique identifier
  text        — text to score against query
  source_type — key for SourceScore tier lookup (optional, defaults to "unknown")
  collected_at — ISO timestamp for staleness (optional)
  metadata    — arbitrary extra fields passed through unchanged

Returns list of RankedCandidate sorted by final_score descending.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

from supervisor.retrieval.bm25 import BM25Index, tokenize as _tok
from supervisor.retrieval.source_confidence import score_source

ALPHA = float(os.getenv("HYBRID_RETRIEVER_ALPHA", "0.6"))   # BM25 weight


@dataclass
class RankedCandidate:
    doc_id: str
    bm25_score: float
    cosine_score: float
    source_confidence: float
    final_score: float
    source_type: str = ""
    age_hours: float = 0.0
    is_stale: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "bm25_score": round(self.bm25_score, 4),
            "cosine_score": round(self.cosine_score, 4),
            "source_confidence": round(self.source_confidence, 4),
            "final_score": round(self.final_score, 4),
            "source_type": self.source_type,
            "age_hours": round(self.age_hours, 1),
            "is_stale": self.is_stale,
            "metadata": self.metadata,
        }


def rank(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int = 10,
    alpha: float = ALPHA,
) -> list[RankedCandidate]:
    """Rank candidates against query using BM25 + cosine + source confidence.

    Args:
        query:      Query string (incident summary, root cause hypothesis, etc.)
        candidates: List of candidate dicts with at minimum {doc_id, text}.
        top_k:      Max results to return.
        alpha:      BM25 weight in [0, 1]. 1-alpha = cosine weight.

    Returns:
        Sorted list of RankedCandidate, best first.
    """
    if not candidates or not query:
        return []

    # Build BM25 index over candidate texts
    docs = [(c["doc_id"], c.get("text", "")) for c in candidates]
    bm25_idx = BM25Index.build(docs)

    # BM25 scores
    bm25_raw = {doc_id: score for doc_id, score in bm25_idx.rank(query, top_k=len(candidates))}

    # Cosine TF-IDF scores (lightweight — no external dep)
    cosine_raw = _cosine_scores(query, candidates)

    # Normalize both score sets to [0, 1]
    bm25_norm = _min_max_norm(bm25_raw)
    cosine_norm = _min_max_norm(cosine_raw)

    # Fuse and apply source confidence
    results = []
    for c in candidates:
        doc_id = c["doc_id"]
        source_type = c.get("source_type", "unknown")
        collected_at = c.get("collected_at")

        src = score_source(source_type, collected_at=collected_at)
        bm25_s = bm25_norm.get(doc_id, 0.0)
        cosine_s = cosine_norm.get(doc_id, 0.0)
        raw = alpha * bm25_s + (1 - alpha) * cosine_s
        final = raw * src.final_confidence

        results.append(RankedCandidate(
            doc_id=doc_id,
            bm25_score=bm25_s,
            cosine_score=cosine_s,
            source_confidence=src.final_confidence,
            final_score=round(final, 4),
            source_type=source_type,
            age_hours=src.age_hours,
            is_stale=src.is_stale(),
            metadata=c.get("metadata", {}),
        ))

    results.sort(key=lambda r: r.final_score, reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cosine_scores(query: str, candidates: list[dict]) -> dict[str, float]:
    """Lightweight TF-IDF cosine similarity — no external deps."""
    q_tokens = _tok(query)
    if not q_tokens:
        return {c["doc_id"]: 0.0 for c in candidates}

    # Build document token sets
    doc_tokens = [(c["doc_id"], _tok(c.get("text", ""))) for c in candidates]
    n = len(doc_tokens)
    if n == 0:
        return {}

    # IDF: log((n+1)/(df+1)) + 1  (sklearn smooth)
    df: dict[str, int] = {}
    for _, tokens in doc_tokens:
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1

    def idf(t: str) -> float:
        return math.log((n + 1) / (df.get(t, 0) + 1)) + 1

    def tfidf(tokens: list[str]) -> dict[str, float]:
        from collections import Counter
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        return {t: (c / total) * idf(t) for t, c in tf.items()}

    q_vec = tfidf(q_tokens)

    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        dot = sum(a.get(t, 0.0) * b.get(t, 0.0) for t in b)
        mag_a = math.sqrt(sum(v * v for v in a.values())) or 1.0
        mag_b = math.sqrt(sum(v * v for v in b.values())) or 1.0
        return dot / (mag_a * mag_b)

    return {
        doc_id: cosine(q_vec, tfidf(tokens))
        for doc_id, tokens in doc_tokens
    }


def _min_max_norm(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a score dict to [0, 1]."""
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    rng = hi - lo
    if rng == 0:
        return {k: 1.0 if hi > 0 else 0.0 for k in scores}
    return {k: (v - lo) / rng for k, v in scores.items()}
