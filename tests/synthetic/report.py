"""SentinelBench deterministic JSON report renderer.

Produces a JSON-safe, sort_keys-friendly summary of a list of
:class:`ScoreCard` objects. Same input → byte-identical output.

Public entry points:
- :func:`render_report`  — list[ScoreCard] → dict
- :func:`render_report_json` — list[ScoreCard] → JSON string
"""
from __future__ import annotations

import json
from statistics import mean
from typing import Any

from tests.synthetic.scoring import DEFAULT_WEIGHTS, ScoreCard


REPORT_SCHEMA_VERSION = 1


def render_report(cards: list[ScoreCard]) -> dict[str, Any]:
    """Return a JSON-safe summary of the given ScoreCards. Deterministic.

    Shape:
        {
          "schema_version": 1,
          "scorecard_count": int,
          "aggregates": {
            "overall_mean": float,
            "overall_min": float,
            "overall_max": float,
            "per_dimension_mean": {
              "root_cause_match": float,
              ...
            },
          },
          "scorecards": [ScoreCard.to_dict(), ...]  # sorted by scenario_id
        }
    """
    sorted_cards = sorted(cards, key=lambda c: c.scenario_id)

    if sorted_cards:
        overall = [c.overall_score for c in sorted_cards]
        per_dim_mean = {
            "root_cause_match":        round(mean(c.root_cause_match       for c in sorted_cards), 4),
            "evidence_completeness":   round(mean(c.evidence_completeness  for c in sorted_cards), 4),
            "red_herring_resistance":  round(mean(c.red_herring_resistance for c in sorted_cards), 4),
            "confidence_calibration":  round(mean(c.confidence_calibration for c in sorted_cards), 4),
            "decision_trace_quality":  round(mean(c.decision_trace_quality for c in sorted_cards), 4),
            "runtime_cost_score":      round(mean(c.runtime_cost_score     for c in sorted_cards), 4),
            "mtti_score":              round(mean(c.mtti_score             for c in sorted_cards), 4),
        }
        aggregates = {
            "overall_mean":       round(mean(overall), 4),
            "overall_min":        round(min(overall),  4),
            "overall_max":        round(max(overall),  4),
            "per_dimension_mean": per_dim_mean,
        }
    else:
        aggregates = {
            "overall_mean":       0.0,
            "overall_min":        0.0,
            "overall_max":        0.0,
            "per_dimension_mean": {k: 0.0 for k in DEFAULT_WEIGHTS.keys()},
        }

    return {
        "schema_version":  REPORT_SCHEMA_VERSION,
        "scorecard_count": len(sorted_cards),
        "aggregates":      aggregates,
        "scorecards":      [c.to_dict() for c in sorted_cards],
    }


def render_report_json(cards: list[ScoreCard], *, indent: int = 2) -> str:
    """Return the report as a deterministic JSON string (sort_keys=True)."""
    return json.dumps(render_report(cards), sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_report",
    "render_report_json",
]
