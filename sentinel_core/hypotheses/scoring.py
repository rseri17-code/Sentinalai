"""Deterministic scoring for Hypothesis Intelligence."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sentinel_core.hypotheses.hypothesis_graph import HypothesisGraph
from sentinel_core.hypotheses.schemas import (
    Hypothesis,
    HypothesisStatus,
    HYPOTHESIS_SCHEMA_VERSION,
)


@dataclass(frozen=True)
class HypothesisScore:
    hypothesis_id:      str
    support_score:      float = 0.0
    refute_score:       float = 0.0
    net_score:          float = 0.0
    confidence_delta:   int = 0
    status:             str = ""
    schema_version:     int = HYPOTHESIS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("support_score", "refute_score", "net_score"):
            d[k] = round(float(d[k]), 4)
        return d


def score_hypothesis(h: Hypothesis) -> HypothesisScore:
    """Deterministic per-hypothesis score. Same input → same output."""
    support = sum(float(e.weight) for e in h.supporting_evidence)
    refute  = sum(float(e.weight) for e in h.refuting_evidence)
    net = support - refute
    if h.transitions:
        first_conf = int(h.transitions[0].confidence_before)
        last_conf  = int(h.transitions[-1].confidence_after)
        delta = last_conf - first_conf
    else:
        delta = 0
    return HypothesisScore(
        hypothesis_id=h.hypothesis_id,
        support_score=round(support, 4),
        refute_score=round(refute, 4),
        net_score=round(net, 4),
        confidence_delta=int(delta),
        status=h.status,
    )


def score_hypothesis_graph(graph: HypothesisGraph) -> tuple[HypothesisScore, ...]:
    """One :class:`HypothesisScore` per hypothesis. Sorted by hypothesis_id."""
    return tuple(sorted(
        (score_hypothesis(h) for h in graph.hypotheses),
        key=lambda s: s.hypothesis_id,
    ))


__all__ = [
    "HypothesisScore",
    "score_hypothesis",
    "score_hypothesis_graph",
]
