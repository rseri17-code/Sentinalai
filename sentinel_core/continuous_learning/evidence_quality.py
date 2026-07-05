"""Evidence quality scoring per evidence key across corpus."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Iterable

from sentinel_core.intel_memory import MemoryRecord


@dataclass(frozen=True)
class EvidenceQualityRow:
    evidence_key:            str
    total_uses:              int
    success_uses:            int
    quality_score:           float   # success_uses / total_uses
    average_mtti_ms:         int
    average_confidence:      int
    schema_version:          int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quality_score"] = round(float(d["quality_score"]), 4)
        return d


class EvidenceQualityScorer:
    """Score each evidence key by success rate + MTTI/confidence context."""

    def score(self, records: Iterable[MemoryRecord]) -> tuple[EvidenceQualityRow, ...]:
        records = tuple(records or ())
        totals: Counter = Counter()
        successes: Counter = Counter()
        per_key_mtti: dict[str, list[int]] = {}
        per_key_conf: dict[str, list[int]] = {}
        for r in records:
            succ = float(r.investigation_score or 0.0) >= 0.5
            for e in r.evidence_collected:
                totals[e] += 1
                if succ:
                    successes[e] += 1
                per_key_mtti.setdefault(e, []).append(int(r.mtti_ms or 0))
                per_key_conf.setdefault(e, []).append(int(r.confidence or 0))
        out: list[EvidenceQualityRow] = []
        for key in sorted(totals.keys()):
            total = totals[key]
            succ = successes.get(key, 0)
            quality = succ / total if total else 0.0
            out.append(EvidenceQualityRow(
                evidence_key=key,
                total_uses=total,
                success_uses=succ,
                quality_score=round(quality, 4),
                average_mtti_ms=int(mean(per_key_mtti[key])) if per_key_mtti[key] else 0,
                average_confidence=int(mean(per_key_conf[key])) if per_key_conf[key] else 0,
            ))
        return tuple(sorted(out,
                              key=lambda r: (-r.quality_score, -r.total_uses,
                                              r.evidence_key)))


__all__ = ["EvidenceQualityRow", "EvidenceQualityScorer"]
