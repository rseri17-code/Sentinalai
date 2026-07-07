"""GuidedInvestigation — top-N similar + aggregated recommendation payload.

Given a query MemoryRecord and a candidate pool, produce a
deterministic dict answering the ten "success metric" questions the
mission asks.
"""
from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any

from sentinel_core.intel_memory.ranking import Ranker
from sentinel_core.intel_memory.schemas import MemoryRecord
from sentinel_core.models._deterministic import canonical_top


GUIDED_SCHEMA_VERSION = 1


class GuidedInvestigation:
    """Deterministic guided-investigation builder.

    Emits a dict answering: have we seen this before, how similar is it,
    what evidence solved it previously, what planner path worked best,
    what investigation order worked best, which evidence was unnecessary,
    what resolution succeeded, how much faster should this investigation
    be, what MTTI to expect, what confidence to expect.
    """

    def __init__(self, ranker: Ranker | None = None, *, top_n: int = 10) -> None:
        self._ranker = ranker or Ranker()
        self._top_n = int(top_n)

    def build(
        self,
        query: MemoryRecord,
        candidates: tuple[MemoryRecord, ...],
    ) -> dict[str, Any]:
        scores = self._ranker.top_n(query, candidates, n=self._top_n)
        # Materialize the top-N candidates for aggregation
        top_ids = {s.memory_id for s in scores}
        top_records = tuple(c for c in candidates if c.memory_id in top_ids)
        top_records = tuple(sorted(top_records, key=lambda r: r.memory_id))

        evidence_overlap = self._evidence_overlap(query, top_records)
        recommended_order = self._recommended_order(top_records)
        recommended_evidence = self._recommended_evidence(top_records)
        recommended_capabilities = self._recommended_capabilities(top_records)
        known_root_causes = self._known_root_causes(top_records)
        known_resolutions = self._known_resolutions(top_records)
        expected_confidence = _avg_int_field(top_records, "confidence")
        expected_mtti = _avg_int_field(top_records, "mtti_ms")
        expected_blast = self._expected_blast_radius(top_records)
        likely_next_step = recommended_order[0] if recommended_order else ""
        previously_successful_sequence = self._successful_sequence(top_records)

        return {
            "schema_version":               GUIDED_SCHEMA_VERSION,
            "have_seen_this_before":        bool(top_records),
            "top_similar":                  [s.to_dict() for s in scores],
            "evidence_overlap":             evidence_overlap,
            "recommended_investigation_order": recommended_order,
            "recommended_evidence":         recommended_evidence,
            "recommended_planner_capabilities": recommended_capabilities,
            "known_root_causes":            known_root_causes,
            "known_resolutions":            known_resolutions,
            "expected_confidence":          expected_confidence,
            "expected_mtti_ms":             expected_mtti,
            "expected_blast_radius":        expected_blast,
            "likely_next_step":             likely_next_step,
            "previously_successful_sequence": previously_successful_sequence,
        }

    # ------------------------------------------------------------------
    # Component builders
    # ------------------------------------------------------------------

    def _evidence_overlap(
        self, query: MemoryRecord, top: tuple[MemoryRecord, ...]
    ) -> dict[str, Any]:
        q = set(query.evidence_collected)
        overlap = 0
        for r in top:
            overlap += len(q & set(r.evidence_collected))
        avg = round(overlap / len(top), 4) if top else 0.0
        return {"average_overlap": avg,
                 "query_evidence":  sorted(q)}

    def _recommended_order(self, top: tuple[MemoryRecord, ...]) -> list[str]:
        # Prefer the ordering that appears most often; else fall back to
        # the flattened intersection of all evidence_ordering tuples.
        seq_counts: Counter = Counter()
        for r in top:
            if r.evidence_ordering:
                seq_counts[r.evidence_ordering] += 1
        if seq_counts:
            # RC-F: canonical tie-break so tied evidence_ordering tuples
            # always pick the same one regardless of caller order.
            most, _ = canonical_top(seq_counts, 1)[0]
            return list(most)
        # Fallback: flatten & dedupe preserving first-seen order
        seen: list[str] = []
        seen_set: set[str] = set()
        for r in sorted(top, key=lambda x: x.memory_id):
            for e in r.evidence_ordering:
                if e not in seen_set:
                    seen.append(e)
                    seen_set.add(e)
        return seen

    def _recommended_evidence(self, top: tuple[MemoryRecord, ...]) -> list[str]:
        counts: Counter = Counter()
        for r in top:
            for e in r.evidence_collected:
                counts[e] += 1
        # Keep evidence appearing in ≥50% of top records (rounded down);
        # for very small pools take everything that appears at all.
        threshold = max(1, len(top) // 2)
        return sorted(e for e, c in counts.items() if c >= threshold)

    def _recommended_capabilities(self, top: tuple[MemoryRecord, ...]) -> list[str]:
        counts: Counter = Counter()
        for r in top:
            for c in r.planner_decisions:
                counts[c] += 1
        return sorted(c for c, n in counts.items() if n >= 1)

    def _known_root_causes(self, top: tuple[MemoryRecord, ...]) -> list[str]:
        rcs = sorted({r.verified_root_cause or r.detected_root_cause
                        for r in top if (r.verified_root_cause
                                          or r.detected_root_cause)})
        return rcs

    def _known_resolutions(self, top: tuple[MemoryRecord, ...]) -> list[str]:
        return sorted({r.resolution for r in top if r.resolution})

    def _expected_blast_radius(self, top: tuple[MemoryRecord, ...]) -> dict[str, Any]:
        if not top:
            return {"severity": "low", "average_affected": 0}
        severity_counts: Counter = Counter(r.blast_radius.severity for r in top)
        # RC-F: canonical tie-break so equally-common severities always
        # resolve to the same one regardless of iteration order.
        most_severity, _ = canonical_top(severity_counts, 1)[0]
        avg_affected = int(mean(r.blast_radius.total_affected for r in top))
        return {"severity": most_severity, "average_affected": avg_affected}

    def _successful_sequence(self, top: tuple[MemoryRecord, ...]) -> list[str]:
        # Use the record with the highest investigation_score as the
        # "previously successful" sequence exemplar.
        if not top:
            return []
        best = max(top, key=lambda r: (r.investigation_score, r.confidence,
                                          -r.mtti_ms))
        return list(best.evidence_ordering) or list(best.evidence_collected)


def _avg_int_field(records: tuple[MemoryRecord, ...], attr: str) -> int:
    if not records:
        return 0
    return int(mean(int(getattr(r, attr, 0) or 0) for r in records))


__all__ = [
    "GUIDED_SCHEMA_VERSION",
    "GuidedInvestigation",
]
