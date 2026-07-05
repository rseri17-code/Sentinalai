"""Retrieval — filter APIs over the MemoryStore.

Every returned tuple is deterministically sorted by ``memory_id`` to
ensure byte-identical output across runs.
"""
from __future__ import annotations

from typing import Iterable

from sentinel_core.intel_memory.memory_store import MemoryStore
from sentinel_core.intel_memory.schemas import MemoryRecord


class Retrieval:
    """Filter-only reader over a :class:`MemoryStore`. Never mutates.

    Every method loads all records once and applies the filter in
    Python — the corpus is expected to be small (<= a few thousand)
    and callers can add caching if needed.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Read-all helper
    # ------------------------------------------------------------------

    def _all(self) -> tuple[MemoryRecord, ...]:
        return self._store.load_all()

    # ------------------------------------------------------------------
    # Individual retrieval APIs
    # ------------------------------------------------------------------

    def by_fingerprint(self, fingerprint: str) -> tuple[MemoryRecord, ...]:
        fp = str(fingerprint or "")
        return _sort(r for r in self._all() if r.fingerprint == fp)

    def by_service(self, service: str) -> tuple[MemoryRecord, ...]:
        s = str(service or "")
        return _sort(r for r in self._all() if r.service == s)

    def by_application(self, application: str) -> tuple[MemoryRecord, ...]:
        a = str(application or "")
        return _sort(r for r in self._all() if r.application == a)

    def by_incident_type(self, incident_type: str) -> tuple[MemoryRecord, ...]:
        t = str(incident_type or "")
        return _sort(r for r in self._all() if r.incident_type == t)

    def by_topology_service(self, service: str) -> tuple[MemoryRecord, ...]:
        """Records whose topology contains ``service`` anywhere."""
        s = str(service or "")
        return _sort(r for r in self._all() if s in r.topology.services)

    def by_deployment(self, deployment_marker: str) -> tuple[MemoryRecord, ...]:
        """Records whose skills_used contains a deployment-related marker
        (e.g. ``git_history:e4c3f023`` or ``deploy:v42``)."""
        m = str(deployment_marker or "")
        return _sort(r for r in self._all() if any(m in s for s in r.skills_used))

    def by_namespace(self, namespace: str) -> tuple[MemoryRecord, ...]:
        n = str(namespace or "")
        return _sort(r for r in self._all() if n in r.topology.namespaces)

    def by_root_cause_contains(self, needle: str) -> tuple[MemoryRecord, ...]:
        n = str(needle or "").lower()
        return _sort(
            r for r in self._all()
            if n and n in r.detected_root_cause.lower()
        )

    def by_transaction_path(self, hop: str) -> tuple[MemoryRecord, ...]:
        h = str(hop or "")
        return _sort(r for r in self._all() if h in r.transaction_path)

    def by_planner_capability(self, capability_id: str) -> tuple[MemoryRecord, ...]:
        c = str(capability_id or "")
        return _sort(r for r in self._all() if c in r.planner_decisions)

    def by_confidence_range(
        self, min_confidence: int = 0, max_confidence: int = 100,
    ) -> tuple[MemoryRecord, ...]:
        lo = int(min_confidence)
        hi = int(max_confidence)
        return _sort(
            r for r in self._all()
            if lo <= r.confidence <= hi
        )

    def by_mtti_range(
        self, min_ms: int = 0, max_ms: int = 10_000_000_000,
    ) -> tuple[MemoryRecord, ...]:
        return _sort(
            r for r in self._all()
            if min_ms <= r.mtti_ms <= max_ms
        )

    def by_time_window(self, start_iso: str, end_iso: str) -> tuple[MemoryRecord, ...]:
        """Records whose ``timestamp`` lies within the (lexicographic)
        window. Empty bounds are treated as open."""
        s = str(start_iso or "")
        e = str(end_iso or "")
        return _sort(
            r for r in self._all()
            if (not s or r.timestamp >= s) and (not e or r.timestamp <= e)
        )


def _sort(records: Iterable[MemoryRecord]) -> tuple[MemoryRecord, ...]:
    return tuple(sorted(records, key=lambda r: r.memory_id))


__all__ = [
    "Retrieval",
]
