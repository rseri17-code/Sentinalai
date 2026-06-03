"""TTL retrieval cache with disk persistence.

Caches retrieval results keyed by (service, incident_type, query_summary).
The harness correction loop can re-query the same incident multiple times —
caching avoids re-executing the full BM25+rerank pipeline each round.

Design:
  - In-memory LRU dict (thread-safe via lock)
  - Optional disk persistence to eval/retrieval_cache.json on shutdown
  - TTL: 300s default — long enough for one harness run (~60s), short enough
    to expire before the next unrelated investigation starts
  - Cache key: sha256(service:incident_type:normalized_query)[:16]
  - Hit/miss recorded in each entry for telemetry tracing

This is NOT a semantic cache — exact key match only. Semantic caching would
require embedding the query, which violates the no-external-dep constraint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sentinalai.retrieval.cache")

_DEFAULT_TTL    = int(os.getenv("RETRIEVAL_CACHE_TTL_SECONDS", "300"))
_DEFAULT_MAX    = int(os.getenv("RETRIEVAL_CACHE_MAX_ENTRIES", "200"))
_DEFAULT_PATH   = os.getenv("RETRIEVAL_CACHE_PATH", "eval/retrieval_cache.json")


@dataclass
class CacheEntry:
    key: str
    results: list[dict]         # serialised RankedCandidate dicts
    stored_at: float            # time.monotonic() timestamp
    ttl: float
    hits: int = 0

    def is_expired(self) -> bool:
        return (time.monotonic() - self.stored_at) > self.ttl

    def touch(self) -> None:
        self.hits += 1


class RetrievalCache:
    """Thread-safe TTL cache for retrieval results."""

    def __init__(
        self,
        ttl: float = _DEFAULT_TTL,
        max_entries: int = _DEFAULT_MAX,
        store_path: str = _DEFAULT_PATH,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._path = store_path
        self._lock = threading.Lock()
        self._store: dict[str, CacheEntry] = {}
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, service: str, incident_type: str, query: str) -> list[dict] | None:
        """Return cached results or None (miss).

        Records hit/miss for telemetry.
        """
        key = _cache_key(service, incident_type, query)
        with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.is_expired():
                if entry:
                    del self._store[key]  # evict expired
                self._misses += 1
                return None
            entry.touch()
            self._hits += 1
            return entry.results

    def put(
        self,
        service: str,
        incident_type: str,
        query: str,
        results: list[Any],
    ) -> str:
        """Store results. Returns the cache key."""
        key = _cache_key(service, incident_type, query)
        serialised = [r.to_dict() if hasattr(r, "to_dict") else r for r in results]
        entry = CacheEntry(
            key=key,
            results=serialised,
            stored_at=time.monotonic(),
            ttl=self._ttl,
        )
        with self._lock:
            self._store[key] = entry
            self._evict_if_needed()
        return key

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "ttl_seconds": self._ttl,
            }

    def flush(self) -> None:
        """Persist cache stats to disk (not full contents — TTL makes content stale)."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
            stats = {"stats": self.stats(), "flushed_at": time.time()}
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(stats, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.debug("RetrievalCache: flush failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Evict expired entries, then LRU if still over max."""
        # Remove expired
        expired = [k for k, e in self._store.items() if e.is_expired()]
        for k in expired:
            del self._store[k]
        # If still over limit, remove least recently used (fewest hits)
        if len(self._store) > self._max:
            sorted_keys = sorted(self._store, key=lambda k: self._store[k].hits)
            for k in sorted_keys[:len(self._store) - self._max]:
                del self._store[k]


def _cache_key(service: str, incident_type: str, query: str) -> str:
    """Stable 16-char hex key. Normalise query to reduce near-duplicates."""
    normalised = " ".join(sorted(query.lower().split()))
    raw = f"{service}:{incident_type}:{normalised}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache: RetrievalCache | None = None
_cache_lock = threading.Lock()


def get_cache() -> RetrievalCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = RetrievalCache()
        return _cache
