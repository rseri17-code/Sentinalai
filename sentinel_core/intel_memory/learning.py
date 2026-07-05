"""LearningLoop — detect recurring patterns across the memory corpus.

Every pattern is a deterministic projection of the memory corpus onto
one dimension. Zero LLM. Zero side effects.
"""
from __future__ import annotations

from statistics import mean
from typing import Iterable

from sentinel_core.intel_memory.schemas import (
    MemoryRecord,
    RecurringPattern,
    RecurringPatternKind,
)


class LearningLoop:
    """Deterministic corpus-wide pattern detector."""

    def __init__(self, *, min_count: int = 2) -> None:
        self._min = max(1, int(min_count))

    # ------------------------------------------------------------------
    # Individual pattern detectors
    # ------------------------------------------------------------------

    def recurring_root_causes(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        return self._by_signature(
            records,
            kind=RecurringPatternKind.ROOT_CAUSE.value,
            signature=lambda r: r.detected_root_cause[:120].strip().lower(),
        )

    def recurring_evidence(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        # Signature = sorted evidence tuple joined; per-record hit yields a
        # single record — we collapse identical evidence sequences.
        return self._by_signature(
            records,
            kind=RecurringPatternKind.EVIDENCE.value,
            signature=lambda r: ",".join(sorted(r.evidence_collected)),
        )

    def recurring_planner_paths(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        return self._by_signature(
            records,
            kind=RecurringPatternKind.PLANNER_PATH.value,
            signature=lambda r: ">".join(r.planner_decisions),
        )

    def recurring_failed_investigations(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        # Failed = investigation_score < 0.5. Signature is the incident_type.
        return self._by_signature(
            [r for r in records if r.investigation_score < 0.5],
            kind=RecurringPatternKind.FAILED_INVESTIGATION.value,
            signature=lambda r: r.incident_type or "",
        )

    def recurring_false_leads(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        seen: dict[str, list[str]] = {}
        for r in records:
            for lead in r.false_leads:
                seen.setdefault(str(lead).lower(), []).append(r.memory_id)
        out = []
        for sig, ids in sorted(seen.items()):
            if len(ids) >= self._min:
                subset = [r for r in records if r.memory_id in ids]
                out.append(RecurringPattern(
                    kind=RecurringPatternKind.FALSE_LEAD.value,
                    signature=sig,
                    count=len(ids),
                    memory_ids=tuple(ids),
                    average_mtti_ms=_avg_int(subset, "mtti_ms"),
                    average_confidence=_avg_int(subset, "confidence"),
                ))
        return tuple(sorted(out, key=lambda p: (-p.count, p.signature)))

    def recurring_missing_evidence(
        self, records: tuple[MemoryRecord, ...],
        canonical_set: Iterable[str] = (),
    ) -> tuple[RecurringPattern, ...]:
        canon = set(str(x) for x in canonical_set)
        missing_per_record: dict[str, list[str]] = {}
        for r in records:
            missing = canon - set(r.evidence_collected)
            for m in missing:
                missing_per_record.setdefault(m, []).append(r.memory_id)
        out = []
        for sig, ids in sorted(missing_per_record.items()):
            if len(ids) >= self._min:
                subset = [r for r in records if r.memory_id in ids]
                out.append(RecurringPattern(
                    kind=RecurringPatternKind.MISSING_EVIDENCE.value,
                    signature=sig,
                    count=len(ids),
                    memory_ids=tuple(ids),
                    average_mtti_ms=_avg_int(subset, "mtti_ms"),
                    average_confidence=_avg_int(subset, "confidence"),
                ))
        return tuple(sorted(out, key=lambda p: (-p.count, p.signature)))

    def recurring_topology_failures(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        return self._by_signature(
            records,
            kind=RecurringPatternKind.TOPOLOGY_FAILURE.value,
            signature=lambda r: ",".join(sorted(r.topology.services)),
        )

    def recurring_transaction_failures(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        return self._by_signature(
            records,
            kind=RecurringPatternKind.TRANSACTION_FAILURE.value,
            signature=lambda r: ">".join(r.transaction_path),
        )

    def recurring_deployment_failures(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        # Signature = incident_type only for records whose skills_used
        # contains a git-history-like marker.
        subset = [r for r in records
                    if any(s.startswith("git_") or "argocd" in s or s.startswith("deploy")
                             for s in r.skills_used)]
        return self._by_signature(
            subset,
            kind=RecurringPatternKind.DEPLOYMENT_FAILURE.value,
            signature=lambda r: r.incident_type or "",
        )

    def recurring_dependency_failures(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        return self._by_signature(
            records,
            kind=RecurringPatternKind.DEPENDENCY_FAILURE.value,
            signature=lambda r: ",".join(sorted(f"{a}>{b}" for a, b
                                                    in r.topology.dependencies)),
        )

    def recurring_blast_radius(
        self, records: tuple[MemoryRecord, ...]
    ) -> tuple[RecurringPattern, ...]:
        return self._by_signature(
            records,
            kind=RecurringPatternKind.BLAST_RADIUS.value,
            signature=lambda r: f"{r.blast_radius.severity}:{r.blast_radius.total_affected}",
        )

    def recurring_mtti_bottlenecks(
        self, records: tuple[MemoryRecord, ...],
        mtti_threshold_ms: int = 120_000,
    ) -> tuple[RecurringPattern, ...]:
        slow = [r for r in records if r.mtti_ms >= mtti_threshold_ms]
        return self._by_signature(
            slow,
            kind=RecurringPatternKind.MTTI_BOTTLENECK.value,
            signature=lambda r: r.incident_type or "",
        )

    def recurring_confidence_drops(
        self, records: tuple[MemoryRecord, ...],
        confidence_threshold: int = 60,
    ) -> tuple[RecurringPattern, ...]:
        low = [r for r in records if r.confidence < confidence_threshold]
        return self._by_signature(
            low,
            kind=RecurringPatternKind.CONFIDENCE_DROP.value,
            signature=lambda r: r.incident_type or "",
        )

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def all_patterns(
        self, records: tuple[MemoryRecord, ...],
        canonical_evidence_set: Iterable[str] = (),
    ) -> tuple[RecurringPattern, ...]:
        """Compute every pattern kind. Deterministic, sorted output."""
        buckets = (
            self.recurring_root_causes(records)
            + self.recurring_evidence(records)
            + self.recurring_planner_paths(records)
            + self.recurring_failed_investigations(records)
            + self.recurring_false_leads(records)
            + self.recurring_missing_evidence(records, canonical_evidence_set)
            + self.recurring_topology_failures(records)
            + self.recurring_transaction_failures(records)
            + self.recurring_deployment_failures(records)
            + self.recurring_dependency_failures(records)
            + self.recurring_blast_radius(records)
            + self.recurring_mtti_bottlenecks(records)
            + self.recurring_confidence_drops(records)
        )
        return tuple(sorted(
            buckets,
            key=lambda p: (p.kind, -p.count, p.signature),
        ))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _by_signature(
        self, records: Iterable[MemoryRecord],
        kind: str,
        signature,
    ) -> tuple[RecurringPattern, ...]:
        buckets: dict[str, list[MemoryRecord]] = {}
        for r in records:
            sig = signature(r)
            if not sig:
                continue
            buckets.setdefault(sig, []).append(r)
        out = []
        for sig in sorted(buckets.keys()):
            group = buckets[sig]
            if len(group) < self._min:
                continue
            out.append(RecurringPattern(
                kind=kind,
                signature=sig,
                count=len(group),
                memory_ids=tuple(sorted(g.memory_id for g in group)),
                average_mtti_ms=_avg_int(group, "mtti_ms"),
                average_confidence=_avg_int(group, "confidence"),
            ))
        return tuple(sorted(out, key=lambda p: (-p.count, p.signature)))


def _avg_int(records: Iterable[MemoryRecord], attr: str) -> int:
    vals = [int(getattr(r, attr, 0) or 0) for r in records]
    return int(mean(vals)) if vals else 0


__all__ = [
    "LearningLoop",
]
