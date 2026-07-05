"""SentinelReplay deterministic JSON report renderers.

Every renderer produces a JSON-safe, sort_keys-friendly dict. Same
input → byte-identical output.
"""
from __future__ import annotations

import json
from statistics import mean
from typing import Any, Iterable

from tests.replay.heatmap import build_heatmap, build_heatmap_series
from tests.replay.learning_engine import LearningEngine
from tests.replay.recommendation_engine import RecommendationEngine
from tests.replay.schemas import (
    BenchmarkRun,
    Recommendation,
    ReplayResult,
    WeaknessRecord,
)
from tests.replay.trend_analysis import (
    build_timeline,
    compute_all_trends,
    detect_improvements,
    detect_regressions,
)


REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Report renderers
# ---------------------------------------------------------------------------

def render_replay_report(results: Iterable[ReplayResult]) -> dict[str, Any]:
    """Deterministic replay report."""
    sorted_results = sorted(results, key=lambda r: r.scenario_id)
    verdicts: dict[str, int] = {}
    for r in sorted_results:
        verdicts[r.verdict] = verdicts.get(r.verdict, 0) + 1
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "result_count":   len(sorted_results),
        "verdicts":       {k: verdicts[k] for k in sorted(verdicts.keys())},
        "results":        [r.to_dict() for r in sorted_results],
    }


def render_trend_report(runs: tuple[BenchmarkRun, ...]) -> dict[str, Any]:
    """Trend report: per-dimension trend + timeline."""
    trends = compute_all_trends(runs)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_count":      len(runs),
        "timeline":       build_timeline(runs),
        "trends":         trends,
    }


def render_regression_report(runs: tuple[BenchmarkRun, ...],
                              threshold: float = 0.05) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "threshold":      round(float(threshold), 4),
        "regressions":    detect_regressions(runs, threshold=threshold),
        "improvements":   detect_improvements(runs, threshold=threshold),
    }


def render_heatmap_report(runs: tuple[BenchmarkRun, ...]) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "series":         build_heatmap_series(runs),
    }


def render_learning_report(runs: tuple[BenchmarkRun, ...],
                            engine: LearningEngine | None = None) -> dict[str, Any]:
    eng = engine or LearningEngine()
    weaknesses = eng.analyze(runs)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_count":      len(runs),
        "weaknesses":     [w.to_dict() for w in weaknesses],
    }


def render_recommendations_report(
    runs: tuple[BenchmarkRun, ...],
    learning: LearningEngine | None = None,
    recommender: RecommendationEngine | None = None,
) -> dict[str, Any]:
    eng = learning or LearningEngine()
    rec = recommender or RecommendationEngine()
    weaknesses = eng.analyze(runs)
    recommendations = rec.recommend(weaknesses)
    return {
        "schema_version":    REPORT_SCHEMA_VERSION,
        "weakness_count":    len(weaknesses),
        "recommendation_count": len(recommendations),
        "recommendations":   [r.to_dict() for r in recommendations],
    }


def render_weakness_leaderboard(
    runs: tuple[BenchmarkRun, ...],
    engine: LearningEngine | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    eng = engine or LearningEngine()
    weaknesses = eng.analyze(runs)
    board = eng.leaderboard(weaknesses, limit=limit)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "limit":          int(limit),
        "leaderboard":    [w.to_dict() for w in board],
    }


# ---------------------------------------------------------------------------
# Master report: everything at once
# ---------------------------------------------------------------------------

def render_master_report(
    runs: tuple[BenchmarkRun, ...],
    results: Iterable[ReplayResult] | None = None,
    regression_threshold: float = 0.05,
) -> dict[str, Any]:
    """Render every replay artifact in one payload."""
    results_seq = tuple(results or ())
    return {
        "schema_version":       REPORT_SCHEMA_VERSION,
        "replay_report":        render_replay_report(results_seq),
        "trend_report":         render_trend_report(runs),
        "regression_report":    render_regression_report(runs, regression_threshold),
        "heatmap_report":       render_heatmap_report(runs),
        "learning_report":      render_learning_report(runs),
        "recommendations_report": render_recommendations_report(runs),
        "weakness_leaderboard": render_weakness_leaderboard(runs),
    }


def to_json(report: dict[str, Any], *, indent: int = 2) -> str:
    """Deterministic JSON encoding."""
    return json.dumps(report, sort_keys=True, indent=indent)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "render_replay_report",
    "render_trend_report",
    "render_regression_report",
    "render_heatmap_report",
    "render_learning_report",
    "render_recommendations_report",
    "render_weakness_leaderboard",
    "render_master_report",
    "to_json",
]
