"""BM25 scorer — Okapi BM25 (Robertson et al., 1994).

Pure Python, no external deps. Used by hybrid_retriever to score
candidates against a query. IDF-weighted term frequency with document
length normalization.

BM25(q, d) = Σ IDF(t) * (tf(t,d) * (k1+1)) / (tf(t,d) + k1*(1 - b + b*|d|/avgdl))
IDF(t)     = log((N - df(t) + 0.5) / (df(t) + 0.5) + 1)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# Standard BM25 hyperparameters
_K1 = 1.5    # term saturation — higher = slower saturation
_B  = 0.75   # length normalization — 0 = no normalization, 1 = full


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, min length 2."""
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2]


@dataclass
class BM25Index:
    """Corpus index for BM25 scoring.

    Build once, score many queries.
    """
    docs: list[list[str]] = field(default_factory=list)          # tokenized docs
    doc_ids: list[str] = field(default_factory=list)             # parallel doc IDs
    df: dict[str, int] = field(default_factory=dict)             # term → doc freq
    avgdl: float = 0.0
    n: int = 0

    @classmethod
    def build(cls, documents: list[tuple[str, str]]) -> "BM25Index":
        """Build index from (doc_id, text) pairs."""
        idx = cls()
        for doc_id, text in documents:
            tokens = tokenize(text)
            idx.docs.append(tokens)
            idx.doc_ids.append(doc_id)
            for t in set(tokens):
                idx.df[t] = idx.df.get(t, 0) + 1
        idx.n = len(idx.docs)
        idx.avgdl = (
            sum(len(d) for d in idx.docs) / idx.n if idx.n else 0.0
        )
        return idx

    def score(self, query: str, doc_index: int) -> float:
        """BM25 score for a single document by its index."""
        if self.n == 0 or doc_index >= len(self.docs):
            return 0.0
        q_tokens = tokenize(query)
        doc = self.docs[doc_index]
        dl = len(doc)
        tf_map = Counter(doc)
        result = 0.0
        for t in q_tokens:
            tf = tf_map.get(t, 0)
            if tf == 0:
                continue
            df = self.df.get(t, 0)
            idf = math.log((self.n - df + 0.5) / (df + 0.5) + 1)
            numerator = tf * (_K1 + 1)
            denominator = tf + _K1 * (1 - _B + _B * dl / max(1, self.avgdl))
            result += idf * numerator / denominator
        return result

    def rank(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Return (doc_id, bm25_score) sorted descending, top_k."""
        scores = [
            (self.doc_ids[i], self.score(query, i))
            for i in range(self.n)
        ]
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]
