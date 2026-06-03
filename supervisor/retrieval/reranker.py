"""Rule-based cross-encoder reranker.

After hybrid retrieval produces an initial ranking, the reranker applies
additional cross-field signals that the first-stage ranker can't see cleanly:

  - Service match:       candidate is for the exact service under investigation (+0.20)
  - Incident type match: candidate shares the incident type (+0.15)
  - Time window:         candidate was collected within the investigation window (+0.10)
  - Recency bonus:       fresher docs get a small boost (normalized, not exponential)
  - Stale penalty:       stale candidates (freshness < 0.5) are pushed down (-0.15)

Final score = hybrid_score * (1 + Σ adjustment)

This is intentionally rule-based, not ML. The signals are deterministic and
auditable — critical for SRE trust in the system.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from supervisor.retrieval.hybrid_retriever import RankedCandidate

_STALE_PENALTY   = float(os.getenv("RERANKER_STALE_PENALTY",    "0.15"))
_SERVICE_BONUS   = float(os.getenv("RERANKER_SERVICE_BONUS",     "0.20"))
_TYPE_BONUS      = float(os.getenv("RERANKER_TYPE_BONUS",        "0.15"))
_WINDOW_BONUS    = float(os.getenv("RERANKER_WINDOW_BONUS",      "0.10"))
_RECENCY_BONUS   = float(os.getenv("RERANKER_RECENCY_MAX_BONUS", "0.05"))

# Age threshold below which recency bonus fully applies (hours)
_RECENCY_WINDOW_HOURS = float(os.getenv("RERANKER_RECENCY_WINDOW_HOURS", "2.0"))


@dataclass
class RerankedCandidate:
    """Candidate after reranking with adjustment breakdown."""
    doc_id: str
    hybrid_score: float
    rerank_score: float
    adjustments: dict[str, float] = field(default_factory=dict)
    source_type: str = ""
    is_stale: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "hybrid_score": round(self.hybrid_score, 4),
            "rerank_score": round(self.rerank_score, 4),
            "adjustments": {k: round(v, 4) for k, v in self.adjustments.items()},
            "source_type": self.source_type,
            "is_stale": self.is_stale,
            "metadata": self.metadata,
        }


def rerank(
    candidates: list[RankedCandidate],
    service: str = "",
    incident_type: str = "",
    investigation_window_hours: float = 24.0,
) -> list[RerankedCandidate]:
    """Rerank candidates using cross-field signals.

    Args:
        candidates:                 Output from hybrid_retriever.rank().
        service:                    Service under investigation (for service match bonus).
        incident_type:              Current incident type (for type match bonus).
        investigation_window_hours: Time window for the window bonus.

    Returns:
        Sorted list of RerankedCandidate, best first.
    """
    if not candidates:
        return []

    results = []
    for c in candidates:
        adjustments: dict[str, float] = {}
        meta = c.metadata

        # Service match bonus
        if service and meta.get("service") == service:
            adjustments["service_match"] = _SERVICE_BONUS

        # Incident type match bonus
        if incident_type and meta.get("incident_type") == incident_type:
            adjustments["incident_type_match"] = _TYPE_BONUS

        # Time window bonus — candidate within investigation window
        if c.age_hours <= investigation_window_hours and c.age_hours >= 0:
            adjustments["within_window"] = _WINDOW_BONUS

        # Recency bonus — linear decay within recency window
        if c.age_hours <= _RECENCY_WINDOW_HOURS:
            fraction = max(0.0, 1.0 - c.age_hours / _RECENCY_WINDOW_HOURS)
            adjustments["recency"] = fraction * _RECENCY_BONUS

        # Stale penalty
        if c.is_stale:
            adjustments["stale_penalty"] = -_STALE_PENALTY

        total_adjustment = sum(adjustments.values())
        rerank_score = max(0.0, c.final_score * (1.0 + total_adjustment))

        results.append(RerankedCandidate(
            doc_id=c.doc_id,
            hybrid_score=c.final_score,
            rerank_score=round(rerank_score, 4),
            adjustments=adjustments,
            source_type=c.source_type,
            is_stale=c.is_stale,
            metadata=meta,
        ))

    results.sort(key=lambda r: r.rerank_score, reverse=True)
    return results
