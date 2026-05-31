"""Cascade Tracker — multi-hop service failure propagation chains.

When service A fails and B fails and C fails in sequence during the same
incident, this module records the A→B→C chain.  Over many incidents it
builds a statistical model of how failures propagate through the graph.

This answers what CoFailureIndex cannot:
  - "When A fails, does it cascade to B first, then C?"
  - "Given A and B have already failed, what fails next?"
  - "What's the typical propagation depth from service X?"

CoFailureIndex tracks pairwise co-failure rates.
CascadeTracker tracks ordered chains of arbitrary depth.

Persists to eval/cascade_tracker.json; writes are atomic via tmp-swap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sentinalai.cascade_tracker")

_DEFAULT_STORE = os.getenv("CASCADE_TRACKER_PATH", "eval/cascade_tracker.json")
_MAX_CHAINS = 2000   # cap to keep the store bounded


@dataclass
class CascadeChain:
    """A recorded multi-hop failure propagation chain.

    Attributes:
        chain:      Ordered tuple of service names (primary first).
        count:      Number of incidents matching this exact chain.
        first_seen: ISO-8601 timestamp of first observation.
        last_seen:  ISO-8601 timestamp of most recent observation.
    """

    chain: list[str]
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""

    @property
    def depth(self) -> int:
        return len(self.chain)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CascadeChain":
        return cls(
            chain=d.get("chain", []),
            count=d.get("count", 0),
            first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""),
        )


def _chain_key(chain: list[str]) -> str:
    return "|".join(chain)


class CascadeTracker:
    """Thread-safe tracker of ordered multi-hop failure chains."""

    def __init__(self, store_path: str = _DEFAULT_STORE) -> None:
        self._path = store_path
        self._lock = threading.Lock()
        self._chains: dict[str, CascadeChain] = {}   # key → CascadeChain
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        primary_service: str,
        ordered_co_failures: list[str],
    ) -> None:
        """Record a cascade starting from *primary_service*.

        Extracts all prefix chains:
          primary → [A]
          primary → [A, B]
          primary → [A, B, C]
          ...

        Args:
            primary_service:    The service that failed first (root).
            ordered_co_failures: Other services, in order of first failure
                                 observation (earliest first).
        """
        if not primary_service or not ordered_co_failures:
            return

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Deduplicate while preserving order
        seen: set[str] = {primary_service}
        ordered: list[str] = []
        for svc in ordered_co_failures:
            if svc and svc != primary_service and svc not in seen:
                seen.add(svc)
                ordered.append(svc)

        if not ordered:
            return

        with self._lock:
            # Record all prefix sub-chains: [primary, A], [primary, A, B], ...
            for depth in range(1, len(ordered) + 1):
                chain = [primary_service] + ordered[:depth]
                key = _chain_key(chain)
                if key not in self._chains:
                    self._chains[key] = CascadeChain(
                        chain=chain,
                        count=1,
                        first_seen=now,
                        last_seen=now,
                    )
                else:
                    self._chains[key].count += 1
                    self._chains[key].last_seen = now

            self._evict_if_needed()
            self._save()

        logger.debug(
            "CascadeTracker: recorded %d sub-chains from %s → %s",
            len(ordered), primary_service, ordered,
        )

    def get_chains_from(
        self,
        service: str,
        min_count: int = 2,
        max_depth: int = 5,
    ) -> list[CascadeChain]:
        """Return chains starting from *service*, sorted by count descending.

        Args:
            service:    Starting service (must be chain[0]).
            min_count:  Minimum observation count to include.
            max_depth:  Maximum chain length to return.
        """
        with self._lock:
            results = [
                c for c in self._chains.values()
                if c.chain and c.chain[0] == service
                and c.count >= min_count
                and c.depth <= max_depth
            ]
        results.sort(key=lambda c: c.count, reverse=True)
        return results

    def get_likely_next(
        self,
        already_failed: list[str],
        min_count: int = 2,
    ) -> list[str]:
        """Given services that have already failed (in order), predict what fails next.

        Looks up chains that share the same ordered prefix as *already_failed*
        and returns candidate next services ranked by observation count.

        Args:
            already_failed: Ordered list of services that have already failed.
            min_count:      Minimum chain count to consider a prediction credible.

        Returns:
            List of predicted next services, most likely first.
        """
        if not already_failed:
            return []

        prefix_key = _chain_key(already_failed)
        candidates: dict[str, int] = {}

        with self._lock:
            for key, chain in self._chains.items():
                if chain.count < min_count:
                    continue
                c = chain.chain
                if len(c) <= len(already_failed):
                    continue
                # Check if the chain starts with already_failed
                if c[:len(already_failed)] == already_failed:
                    next_svc = c[len(already_failed)]
                    candidates[next_svc] = candidates.get(next_svc, 0) + chain.count

        return sorted(candidates, key=lambda s: candidates[s], reverse=True)

    def get_summary(self) -> dict[str, Any]:
        """Return overall cascade statistics."""
        with self._lock:
            total = len(self._chains)
            if not total:
                return {"total_chains": 0}
            max_depth = max((c.depth for c in self._chains.values()), default=0)
            most_common = sorted(
                self._chains.values(), key=lambda c: c.count, reverse=True
            )[:5]
            return {
                "total_chains": total,
                "max_depth": max_depth,
                "most_common": [
                    {"chain": c.chain, "count": c.count} for c in most_common
                ],
            }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._chains = {
                _chain_key(r.get("chain", [])): CascadeChain.from_dict(r)
                for r in raw
                if isinstance(r, dict) and r.get("chain")
            }
            logger.info(
                "CascadeTracker loaded %d chains from %s",
                len(self._chains), self._path,
            )
        except FileNotFoundError:
            self._chains = {}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("CascadeTracker load failed: %s — starting empty", exc)
            self._chains = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(
                    [c.to_dict() for c in self._chains.values()],
                    f, indent=2,
                )
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("CascadeTracker save failed: %s", exc)

    def _evict_if_needed(self) -> None:
        if len(self._chains) <= _MAX_CHAINS:
            return
        # Evict least-seen chains first
        sorted_keys = sorted(self._chains, key=lambda k: self._chains[k].count)
        for key in sorted_keys[:len(self._chains) - _MAX_CHAINS]:
            del self._chains[key]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_tracker: CascadeTracker | None = None
_tracker_lock = threading.Lock()


def get_cascade_tracker(store_path: str = _DEFAULT_STORE) -> CascadeTracker:
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = CascadeTracker(store_path)
        return _tracker
