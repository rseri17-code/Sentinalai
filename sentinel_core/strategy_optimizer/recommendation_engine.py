"""StrategyRecommendationEngine — emit deterministic recommendations."""
from __future__ import annotations

from typing import Iterable

from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.strategy_optimizer.schemas import (
    StrategyRecommendation,
    StrategyRecommendationKind,
)
from sentinel_core.strategy_optimizer.strategy_graph import StrategyGraph


# Baseline heuristics
_LOW_USE_THRESHOLD    = 0.15   # evidence used in fewer than 15% of runs
_HIGH_USE_THRESHOLD   = 0.60   # evidence used in ≥ 60% of runs
_LOW_SUCCESS_THRESHOLD = 0.30   # capability succeeded in fewer than 30% of runs


class StrategyRecommendationEngine:
    """Deterministic recommender. Every recommendation includes an
    evidence tuple explaining WHY (mission requirement)."""

    def recommend(
        self,
        records: Iterable[MemoryRecord],
        graph: StrategyGraph | None = None,
    ) -> tuple[StrategyRecommendation, ...]:
        recs: list[StrategyRecommendation] = []
        records = tuple(records or ())
        g = graph or StrategyGraph().ingest(records)
        total = max(1, g.records_seen())

        # 1. Recommended order — top-K evidence transitions
        transitions = g.evidence_transitions(limit=5)
        for (a, b), count in transitions:
            if count >= 2:
                recs.append(StrategyRecommendation(
                    kind=StrategyRecommendationKind.RECOMMENDED_ORDER.value,
                    message=f"Collect '{a}' before '{b}' (seen {count} times).",
                    evidence=(f"transition_count={count}",
                                f"before={a}", f"after={b}"),
                    priority=200 + count * 10,
                ))

        # 2. Skip evidence — very-low-usage evidence
        for evd, cnt in g.top_evidence(limit=30):
            frac = cnt / total
            if frac < _LOW_USE_THRESHOLD:
                recs.append(StrategyRecommendation(
                    kind=StrategyRecommendationKind.SKIP_EVIDENCE.value,
                    message=f"Consider skipping '{evd}' (used in "
                              f"{round(frac*100, 1)}% of investigations).",
                    evidence=(f"usage_rate={round(frac, 4)}",
                                f"seen_in={cnt}/{total}",
                                f"evidence_key={evd}"),
                    priority=150,
                ))

        # 3. Avoid capability — low-success capabilities
        for cap, cnt in g.top_capabilities(limit=30):
            success = g.capability_success_rate(cap)
            if cnt >= 2 and success < _LOW_SUCCESS_THRESHOLD:
                recs.append(StrategyRecommendation(
                    kind=StrategyRecommendationKind.AVOID_CAPABILITY.value,
                    message=(f"'{cap}' has {round(success*100,1)}% historical "
                              f"success — consider avoiding as a first step."),
                    evidence=(f"success_rate={success}",
                                f"seen_in={cnt}/{total}",
                                f"capability={cap}"),
                    related_capabilities=(cap,),
                    priority=180 + int((1.0 - success) * 100),
                ))

        # 4. Prefer capability — high-success + high-use capabilities
        for cap, cnt in g.top_capabilities(limit=30):
            success = g.capability_success_rate(cap)
            frac = cnt / total
            if frac >= _HIGH_USE_THRESHOLD and success >= 0.7:
                recs.append(StrategyRecommendation(
                    kind=StrategyRecommendationKind.PREFER_CAPABILITY.value,
                    message=(f"'{cap}' is a high-value first step: "
                              f"{round(success*100,1)}% success in "
                              f"{round(frac*100,1)}% of investigations."),
                    evidence=(f"success_rate={success}",
                                f"usage_rate={round(frac,4)}",
                                f"capability={cap}"),
                    related_capabilities=(cap,),
                    priority=300 + int(success * 100),
                ))

        # 5. Prefer tool — recommend the highest-success capability's
        # first-listed skill (derived from planner path).
        cap_transitions = g.most_common_transitions(limit=3)
        for (cap_a, cap_b), count in cap_transitions:
            if count >= 2:
                recs.append(StrategyRecommendation(
                    kind=StrategyRecommendationKind.PREFER_TOOL.value,
                    message=(f"Investigate '{cap_a}' before '{cap_b}' — this "
                              f"sequence appeared in {count} investigations."),
                    evidence=(f"sequence_count={count}",
                                f"first={cap_a}", f"second={cap_b}"),
                    priority=220,
                    related_capabilities=(cap_a, cap_b),
                ))

        # Deterministic sort: priority DESC then message
        recs.sort(key=lambda r: (-r.priority, r.kind, r.message))
        return tuple(recs)


__all__ = ["StrategyRecommendationEngine"]
