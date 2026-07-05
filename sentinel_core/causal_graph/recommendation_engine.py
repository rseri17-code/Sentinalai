"""CausalRecommendationEngine — deterministic causal-graph recommendations."""
from __future__ import annotations

from enum import Enum
from typing import Iterable

from sentinel_core.causal_graph.chain_detector import ChainDetector
from sentinel_core.causal_graph.mtti_paths import MTTIPathRanker
from sentinel_core.causal_graph.rca_paths import RCAPathRanker
from sentinel_core.causal_graph.recurrence import RecurrenceDetector
from sentinel_core.causal_graph.schemas import (
    CausalChain,
    CausalRecommendation,
    MTTIPath,
    RCAPath,
)
from sentinel_core.intel_memory import MemoryRecord


class CausalRecommendationKind(str, Enum):
    RECURRING_ROOT_CAUSE   = "recurring_root_cause"
    FASTEST_RCA_PATH       = "fastest_rca_path"
    HIGH_RISK_SERVICE      = "high_risk_service"
    RELIABLE_REMEDIATION   = "reliable_remediation"
    EVIDENCE_TO_PROVE      = "evidence_to_prove"
    FALSE_LEAD_TO_SKIP     = "false_lead_to_skip"


class CausalRecommendationEngine:
    """Deterministic recommender. Every recommendation includes an
    evidence tuple explaining WHY."""

    def recommend(
        self, records: Iterable[MemoryRecord],
    ) -> tuple[CausalRecommendation, ...]:
        records = tuple(records or ())
        chains = ChainDetector().detect(records)
        rca_paths = RCAPathRanker().build(records)
        mtti_paths = MTTIPathRanker().build(records)
        recurrences = RecurrenceDetector().all_recurrences(records)

        recs: list[CausalRecommendation] = []

        # 1. Recurring root causes
        for rec in recurrences:
            if rec.kind != "root_cause":
                continue
            recs.append(CausalRecommendation(
                kind=CausalRecommendationKind.RECURRING_ROOT_CAUSE.value,
                message=(f"Root cause '{rec.signature}' has recurred "
                          f"{rec.count} times."),
                evidence=(f"count={rec.count}",
                            f"average_mtti_ms={rec.average_mtti_ms}",
                            f"average_confidence={rec.average_confidence}"),
                priority=200 + rec.count * 20,
                related_root_causes=(rec.signature,),
            ))

        # 2. Fastest RCA paths — top-3 lowest-MTTI paths
        for mp in mtti_paths[:3]:
            if not mp.root_cause:
                continue
            recs.append(CausalRecommendation(
                kind=CausalRecommendationKind.FASTEST_RCA_PATH.value,
                message=(f"For '{mp.service}', fastest path to RCA "
                          f"'{mp.root_cause[:60]}' averages "
                          f"{mp.average_mtti_ms} ms."),
                evidence=(f"average_mtti_ms={mp.average_mtti_ms}",
                            f"best_mtti_ms={mp.best_mtti_ms}",
                            f"evidence_ordering={list(mp.evidence_ordering)}"),
                priority=300,
                related_services=(mp.service,) if mp.service else (),
                related_root_causes=(mp.root_cause,),
            ))

        # 3. High-risk services (recurring service failures)
        for rec in recurrences:
            if rec.kind != "service":
                continue
            if rec.count < 3:
                continue
            recs.append(CausalRecommendation(
                kind=CausalRecommendationKind.HIGH_RISK_SERVICE.value,
                message=(f"Service '{rec.signature}' has been the subject "
                          f"of {rec.count} investigations."),
                evidence=(f"count={rec.count}",
                            f"average_mtti_ms={rec.average_mtti_ms}"),
                priority=150 + rec.count * 5,
                related_services=(rec.signature,),
            ))

        # 4. Reliable remediation
        for rec in recurrences:
            if rec.kind != "remediation" or rec.count < 2:
                continue
            recs.append(CausalRecommendation(
                kind=CausalRecommendationKind.RELIABLE_REMEDIATION.value,
                message=(f"Remediation '{rec.signature[:60]}' has resolved "
                          f"{rec.count} similar incidents."),
                evidence=(f"count={rec.count}",),
                priority=180 + rec.count * 10,
            ))

        # 5. Evidence-to-prove — evidence keys that appear in confirmed RCA paths
        seen_evidence: dict[str, int] = {}
        for path in rca_paths:
            if path.confidence < 0.6:
                continue
            for e in path.evidence_keys:
                seen_evidence[e] = seen_evidence.get(e, 0) + 1
        for e, cnt in sorted(seen_evidence.items(), key=lambda x: (-x[1], x[0]))[:5]:
            if cnt < 2:
                continue
            recs.append(CausalRecommendation(
                kind=CausalRecommendationKind.EVIDENCE_TO_PROVE.value,
                message=(f"Evidence '{e}' appears in {cnt} high-confidence "
                          f"RCA paths — collect early."),
                evidence=(f"appearances={cnt}", f"evidence_key={e}"),
                priority=140,
            ))

        # 6. False leads to skip — collect from every record's false_leads
        fl_counts: dict[str, int] = {}
        for r in records:
            for fl in r.false_leads:
                fl_counts[fl] = fl_counts.get(fl, 0) + 1
        for fl, cnt in sorted(fl_counts.items(), key=lambda x: (-x[1], x[0]))[:5]:
            if cnt < 2:
                continue
            recs.append(CausalRecommendation(
                kind=CausalRecommendationKind.FALSE_LEAD_TO_SKIP.value,
                message=f"'{fl}' has been a false lead {cnt} times — skip.",
                evidence=(f"false_lead_count={cnt}", f"lead={fl}"),
                priority=130,
            ))

        recs.sort(key=lambda r: (-r.priority, r.kind, r.message))
        return tuple(recs)


__all__ = [
    "CausalRecommendationKind",
    "CausalRecommendationEngine",
]
