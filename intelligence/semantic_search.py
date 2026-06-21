"""Semantic search index backed by TF-IDF + cosine similarity.

SEMANTIC_BACKEND env var:
  "tfidf"       — default, uses sklearn TfidfVectorizer
  "passthrough" — stub for future real-embedding backends
"""
from __future__ import annotations

import os

_BACKEND = os.environ.get("SEMANTIC_BACKEND", "tfidf")


class SemanticIndex:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._texts: list[str] = []
        self._dirty = False
        self._vectorizer = None
        self._matrix = None

    def add(self, id: str, text: str) -> None:
        self._ids.append(id)
        self._texts.append(text)
        self._dirty = True

    def _fit(self) -> None:
        if not self._texts:
            self._vectorizer = None
            self._matrix = None
            self._dirty = False
            return
        if _BACKEND == "passthrough":
            self._dirty = False
            return
        from sklearn.feature_extraction.text import TfidfVectorizer
        self._vectorizer = TfidfVectorizer(sublinear_tf=True)
        self._matrix = self._vectorizer.fit_transform(self._texts)
        self._dirty = False

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        if not self._ids:
            return []
        if self._dirty:
            self._fit()
        if _BACKEND == "passthrough" or self._vectorizer is None:
            return [(id, 0.0) for id in self._ids[:top_k]]
        from sklearn.metrics.pairwise import cosine_similarity
        q_vec = self._vectorizer.transform([query])
        scores = cosine_similarity(q_vec, self._matrix)[0]
        ranked = sorted(
            ((self._ids[i], float(scores[i])) for i in range(len(self._ids))),
            key=lambda x: -x[1],
        )
        return ranked[:top_k]

    def clear(self) -> None:
        self.__init__()
