"""Investigation strategy graph — evidence + capability co-occurrence."""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.models._deterministic import canonical_top


class StrategyGraph:
    """Compact directed co-occurrence graph derived from a MemoryRecord
    corpus. Deterministic. No mutation of the corpus."""

    def __init__(self) -> None:
        self._capability_counts: Counter = Counter()
        self._evidence_counts:   Counter = Counter()
        self._cap_evidence_pairs: Counter = Counter()   # (cap, evidence)
        self._cap_transitions:    Counter = Counter()   # (cap_from, cap_to)
        self._evidence_transitions: Counter = Counter() # (evd_from, evd_to)
        self._records_seen:      int = 0
        self._records_success:   int = 0                # investigation_score >= 0.5
        self._cap_success:       Counter = Counter()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def ingest(self, records: Iterable[MemoryRecord]) -> "StrategyGraph":
        for r in (records or ()):
            self._records_seen += 1
            if r.investigation_score >= 0.5:
                self._records_success += 1
            for c in r.planner_decisions:
                self._capability_counts[c] += 1
                if r.investigation_score >= 0.5:
                    self._cap_success[c] += 1
            for e in r.evidence_collected:
                self._evidence_counts[e] += 1
            # Cap ↔ evidence
            for c in r.planner_decisions:
                for e in r.evidence_collected:
                    self._cap_evidence_pairs[(c, e)] += 1
            # Adjacent transitions
            for i in range(1, len(r.planner_decisions)):
                self._cap_transitions[(r.planner_decisions[i - 1],
                                          r.planner_decisions[i])] += 1
            for i in range(1, len(r.evidence_ordering)):
                self._evidence_transitions[(r.evidence_ordering[i - 1],
                                              r.evidence_ordering[i])] += 1
        return self

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def capability_count(self, cap: str) -> int:
        return self._capability_counts.get(cap, 0)

    def capability_success_rate(self, cap: str) -> float:
        n = self._capability_counts.get(cap, 0)
        if n == 0:
            return 0.0
        return round(self._cap_success.get(cap, 0) / n, 4)

    def evidence_count(self, key: str) -> int:
        return self._evidence_counts.get(key, 0)

    def top_capabilities(self, limit: int = 10) -> list[tuple[str, int]]:
        # RC-F: canonical tie-break by capability_id.
        return canonical_top(self._capability_counts, int(limit))

    def top_evidence(self, limit: int = 10) -> list[tuple[str, int]]:
        # RC-F: canonical tie-break by evidence key.
        return canonical_top(self._evidence_counts, int(limit))

    def most_common_transitions(self, limit: int = 10) -> list[tuple[tuple[str, str], int]]:
        # RC-F: canonical tie-break by transition tuple (lex on the
        # (from, to) pair — well-defined for string tuples).
        return canonical_top(self._cap_transitions, int(limit))

    def evidence_transitions(self, limit: int = 10) -> list[tuple[tuple[str, str], int]]:
        # RC-F: same policy — canonical tie-break by (from, to) tuple.
        return canonical_top(self._evidence_transitions, int(limit))

    def records_seen(self) -> int:
        return self._records_seen

    def records_success(self) -> int:
        return self._records_success


__all__ = ["StrategyGraph"]
