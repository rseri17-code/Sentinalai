"""Local TF-IDF vector index for semantic search across wiki notes.

Pure Python — no sklearn, no numpy, no external deps.
Updated incrementally as notes are ingested.
Persisted to sentinel_wiki/indexes/vector_index.json.

This gives the wiki semantic retrieval capability: "memory leak" matches
"memory exhaustion", "timeout cascade" surfaces latency-related receipts.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("sentinalai.wiki.vector_index")

_INDEX_FILE = "indexes/vector_index.json"

# Common English stop words to exclude from TF-IDF
_STOP_WORDS = frozenset({
    "the", "and", "for", "this", "that", "with", "from", "are", "was",
    "were", "has", "have", "had", "been", "will", "not", "all", "but",
    "can", "its", "per", "any", "new", "also", "one", "two", "use",
    "set", "get", "run", "via", "may", "see", "note", "auto", "none",
    "true", "false", "null", "yes", "unknown",
})
_MIN_TOKEN_LEN = 3


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, filter stop words and short tokens."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOP_WORDS]


def build_tf(tokens: list[str]) -> dict[str, float]:
    """Term frequency: count / total tokens."""
    if not tokens:
        return {}
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    n = len(tokens)
    return {t: c / n for t, c in counts.items()}


def cosine_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two sparse TF-IDF vectors."""
    if not a or not b:
        return 0.0
    dot = sum(a.get(t, 0.0) * b.get(t, 0.0) for t in b)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


class WikiVectorIndex:
    """TF-IDF index over wiki notes and receipts.

    Structure stored on disk:
      {
        "docs": {note_path: {token: tf_score}},
        "df":   {token: doc_frequency},
        "n_docs": int
      }

    IDF is computed at query time from df and n_docs, not stored,
    to keep the on-disk format stable when new docs are added.
    """

    def __init__(self, base_path: str = "sentinel_wiki") -> None:
        self._root = Path(base_path)
        self._path = self._root / _INDEX_FILE
        self._docs: dict[str, dict[str, float]] = {}  # note → {token: tf}
        self._df: dict[str, int] = {}                 # token → doc_count
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_note(self, note_path: str, text: str) -> None:
        """Add or update a note in the index."""
        tokens = tokenize(text)
        tf = build_tf(tokens)

        # Remove old df contributions for this note
        if note_path in self._docs:
            for t in self._docs[note_path]:
                self._df[t] = max(0, self._df.get(t, 1) - 1)
                if self._df[t] == 0:
                    del self._df[t]

        self._docs[note_path] = tf

        # Update df
        for t in tf:
            self._df[t] = self._df.get(t, 0) + 1

    def remove_note(self, note_path: str) -> None:
        """Remove a note from the index."""
        if note_path not in self._docs:
            return
        for t in self._docs[note_path]:
            self._df[t] = max(0, self._df.get(t, 1) - 1)
            if self._df[t] == 0:
                del self._df[t]
        del self._docs[note_path]

    def search(self, query: str, top_k: int = 10, min_score: float = 0.05) -> list[dict]:
        """Semantic search using TF-IDF cosine similarity.

        Returns list of {note_path, score} dicts, sorted by score desc.
        """
        if not query or not self._docs:
            return []

        n = len(self._docs)
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        # Build query TF-IDF vector
        q_tf = build_tf(query_tokens)
        q_tfidf = self._apply_idf(q_tf, n)

        results = []
        for note_path, doc_tf in self._docs.items():
            doc_tfidf = self._apply_idf(doc_tf, n)
            score = cosine_similarity(q_tfidf, doc_tfidf)
            if score >= min_score:
                results.append({"note_path": note_path, "score": round(score, 4)})

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]

    def save(self) -> None:
        """Persist index to disk atomically."""
        self._root / "indexes"
        try:
            (self._root / "indexes").mkdir(parents=True, exist_ok=True)
            data = {
                "docs": self._docs,
                "df": self._df,
                "n_docs": len(self._docs),
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("WikiVectorIndex: save failed: %s", exc)

    def stats(self) -> dict[str, Any]:
        return {
            "indexed_docs": len(self._docs),
            "unique_tokens": len(self._df),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply_idf(self, tf: dict[str, float], n: int) -> dict[str, float]:
        """Multiply TF by IDF = log((n+1) / (df+1)) + 1  (sklearn-style smooth)."""
        if n == 0:
            return tf
        result = {}
        for t, tf_score in tf.items():
            df = self._df.get(t, 0)
            idf = math.log((n + 1) / (df + 1)) + 1
            result[t] = tf_score * idf
        return result

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            self._docs = data.get("docs", {})
            self._df = data.get("df", {})
        except (FileNotFoundError, json.JSONDecodeError):
            self._docs = {}
            self._df = {}
