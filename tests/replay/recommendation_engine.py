"""Recommendation engine — deterministic mapping WeaknessRecord →
Recommendation.

Every recommendation includes evidence explaining WHY it was produced
(mission requirement). No side effects. No production code touched.
"""
from __future__ import annotations

from tests.replay.schemas import (
    Recommendation,
    RecommendationKind,
    WeaknessRecord,
    WeaknessType,
)


# WeaknessType → RecommendationKind mapping. Each dimension of concern
# has a canonical recommendation kind.
_WEAKNESS_TO_KIND: dict[str, str] = {
    WeaknessType.MISSING_EVIDENCE.value:         RecommendationKind.RECOMMENDED_COLLECTOR.value,
    WeaknessType.PLANNER_MISTAKE.value:          RecommendationKind.RECOMMENDED_PLANNER_CAP.value,
    WeaknessType.LOW_CONFIDENCE.value:           RecommendationKind.RECOMMENDED_RCA_PATTERN.value,
    WeaknessType.FALSE_POSITIVE.value:           RecommendationKind.RECOMMENDED_SCENARIO.value,
    WeaknessType.FALSE_NEGATIVE.value:           RecommendationKind.RECOMMENDED_SCENARIO.value,
    WeaknessType.INCORRECT_ROOT_CAUSE.value:     RecommendationKind.RECOMMENDED_RCA_PATTERN.value,
    WeaknessType.MISSING_TOPOLOGY.value:         RecommendationKind.RECOMMENDED_TOPOLOGY.value,
    WeaknessType.MISSING_DEPENDENCY.value:       RecommendationKind.RECOMMENDED_TOPOLOGY.value,
    WeaknessType.BLAST_RADIUS_MISTAKE.value:     RecommendationKind.RECOMMENDED_KG_ENTITY.value,
    WeaknessType.TRANSACTION_PATH_GAP.value:     RecommendationKind.RECOMMENDED_TX_MAPPING.value,
    WeaknessType.RECURRING_INCIDENT_CLASS.value: RecommendationKind.RECOMMENDED_MTTI_IMPROVE.value,
}


# Short human-readable summary keyed by weakness type.
_MESSAGE_TEMPLATES: dict[str, str] = {
    WeaknessType.MISSING_EVIDENCE.value:
        "Add or activate a collector to fill the '{dimension}' evidence gap in '{scenario_id}'.",
    WeaknessType.PLANNER_MISTAKE.value:
        "Planner is not selecting a capability that satisfies '{scenario_id}' — review planner_rules "
        "for '{dimension}'.",
    WeaknessType.LOW_CONFIDENCE.value:
        "'{scenario_id}' is repeatedly outside its expected confidence range — review the RCA pattern "
        "and DecisionContext signals.",
    WeaknessType.FALSE_POSITIVE.value:
        "'{scenario_id}' keeps naming red-herring evidence — add a discriminating benchmark scenario.",
    WeaknessType.FALSE_NEGATIVE.value:
        "'{scenario_id}' keeps missing the correct signal — add a discriminating benchmark scenario.",
    WeaknessType.INCORRECT_ROOT_CAUSE.value:
        "RCA for '{scenario_id}' repeatedly diverges from ground truth — extract a new RCA pattern.",
    WeaknessType.MISSING_TOPOLOGY.value:
        "Topology data is missing for '{scenario_id}' — enrich the KG with the affected entities.",
    WeaknessType.MISSING_DEPENDENCY.value:
        "Dependencies are missing for '{scenario_id}' — add the upstream/downstream edges.",
    WeaknessType.BLAST_RADIUS_MISTAKE.value:
        "Blast radius for '{scenario_id}' is under-modeled — add the missing KG entities.",
    WeaknessType.TRANSACTION_PATH_GAP.value:
        "Transaction path unknown for '{scenario_id}' — add transaction mapping.",
    WeaknessType.RECURRING_INCIDENT_CLASS.value:
        "'{scenario_id}' is recurring on '{dimension}' — prioritise MTTI/cost improvements for this class.",
}


class RecommendationEngine:
    """Deterministic mapping WeaknessRecord → Recommendation."""

    def recommend(
        self, weaknesses: tuple[WeaknessRecord, ...] | list[WeaknessRecord]
    ) -> tuple[Recommendation, ...]:
        """Emit one Recommendation per (kind, dimension, scenario_id)
        weakness cluster. Deterministic ordering."""
        by_key: dict[tuple[str, str, str], list[WeaknessRecord]] = {}
        for w in (weaknesses or ()):
            key = (w.weakness_type, w.dimension, w.scenario_id)
            by_key.setdefault(key, []).append(w)

        out: list[Recommendation] = []
        for key in sorted(by_key.keys()):
            wtype, dim, scenario_id = key
            group = by_key[key]
            kind = _WEAKNESS_TO_KIND.get(
                wtype, RecommendationKind.RECOMMENDED_RCA_PATTERN.value
            )
            template = _MESSAGE_TEMPLATES.get(
                wtype,
                "Investigate repeated weakness for '{scenario_id}' on '{dimension}'.",
            )
            message = template.format(scenario_id=scenario_id, dimension=dim)

            avg = round(
                sum(w.average_score for w in group) / len(group), 4
            )
            count = sum(w.count for w in group)

            # Priority = higher when count is larger + score is lower
            priority = min(1000, 100 + count * 50 + int((1.0 - avg) * 100))

            evidence = tuple(sorted({
                f"count={count}",
                f"average_score={avg}",
                f"weakness_type={wtype}",
                f"dimension={dim}",
            }))
            related = tuple(sorted({w.scenario_id for w in group}))

            out.append(Recommendation(
                kind=kind,
                message=message,
                evidence=evidence,
                priority=priority,
                related_scenarios=related,
            ))

        # Highest priority first; tie-break by (kind, message).
        out.sort(key=lambda r: (-r.priority, r.kind, r.message))
        return tuple(out)


__all__ = [
    "RecommendationEngine",
]
