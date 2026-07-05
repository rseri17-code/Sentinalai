"""SentinelReplay — trend_analysis + heatmap tests."""
from __future__ import annotations

import json

import pytest

from tests.replay.heatmap import build_heatmap, build_heatmap_series
from tests.replay.schemas import BenchmarkRun
from tests.replay.trend_analysis import (
    build_timeline,
    compute_all_trends,
    compute_dimension_trend,
    compute_trend,
    detect_improvements,
    detect_regressions,
)
from tests.synthetic.scoring import ScoreCard


def _card(scenario_id: str, overall: float = 1.0, **overrides) -> ScoreCard:
    defaults = dict(
        root_cause_match=overall,
        evidence_completeness=overall,
        red_herring_resistance=overall,
        confidence_calibration=overall,
        decision_trace_quality=overall,
        runtime_cost_score=overall,
        mtti_score=overall,
        overall_score=overall,
    )
    defaults.update(overrides)
    return ScoreCard(scenario_id=scenario_id, **defaults)


def _run(rid: str, at: str, cards) -> BenchmarkRun:
    return BenchmarkRun(run_id=rid, generated_at=at, scorecards=tuple(cards))


# ---------------------------------------------------------------------------
# compute_trend
# ---------------------------------------------------------------------------

class TestComputeTrend:
    def test_empty(self):
        t = compute_trend([])
        assert t["direction"] == "stable"
        assert t["count"] == 0
        assert t["mean"] == 0.0

    def test_single(self):
        t = compute_trend([0.5])
        assert t["direction"] == "stable"
        assert t["slope"] == 0.0
        assert t["mean"] == 0.5

    def test_upward(self):
        t = compute_trend([0.1, 0.4, 0.7, 1.0])
        assert t["direction"] == "up"
        assert t["slope"] > 0.01

    def test_downward(self):
        t = compute_trend([1.0, 0.7, 0.4, 0.1])
        assert t["direction"] == "down"
        assert t["slope"] < -0.01

    def test_stable_within_deadband(self):
        # slope = 0.005 < deadband 0.01
        t = compute_trend([0.500, 0.505])
        assert t["direction"] == "stable"

    def test_deterministic(self):
        v = [0.1, 0.4, 0.7, 1.0]
        assert compute_trend(v) == compute_trend(v)


# ---------------------------------------------------------------------------
# compute_dimension_trend + compute_all_trends
# ---------------------------------------------------------------------------

class TestDimensionTrends:
    def test_dimension_trend_upward(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", overall=0.4)]),
            _run("r2", "2026-07-02", [_card("s1", overall=0.7)]),
            _run("r3", "2026-07-03", [_card("s1", overall=1.0)]),
        )
        t = compute_dimension_trend(runs, "overall_score")
        assert t["direction"] == "up"

    def test_unknown_dimension_rejected(self):
        runs = (_run("r1", "", [_card("s1")]),)
        with pytest.raises(ValueError):
            compute_dimension_trend(runs, "not_a_dimension")

    def test_compute_all_trends_covers_every_dimension(self):
        runs = (_run("r1", "", [_card("s1")]),)
        trends = compute_all_trends(runs)
        for d in ("root_cause_match", "evidence_completeness",
                    "red_herring_resistance", "confidence_calibration",
                    "decision_trace_quality", "runtime_cost_score",
                    "mtti_score", "overall_score"):
            assert d in trends


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

class TestTimeline:
    def test_timeline_sorted_by_generated_at(self):
        runs = (
            _run("b", "2026-07-02", [_card("s1")]),
            _run("a", "2026-07-01", [_card("s1")]),
        )
        tl = build_timeline(runs)
        assert tl[0]["run_id"] == "a"
        assert tl[1]["run_id"] == "b"

    def test_empty_scorecards_yield_zeroes(self):
        runs = (_run("r1", "", []),)
        tl = build_timeline(runs)
        assert tl[0]["scorecard_count"] == 0
        assert tl[0]["overall_score"] == 0.0


# ---------------------------------------------------------------------------
# Regression + improvement detection
# ---------------------------------------------------------------------------

class TestRegressionsAndImprovements:
    def test_no_change_no_flags(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", overall=0.9)]),
            _run("r2", "2026-07-02", [_card("s1", overall=0.9)]),
        )
        assert detect_regressions(runs) == []
        assert detect_improvements(runs) == []

    def test_regression_detected(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", overall=1.0)]),
            _run("r2", "2026-07-02", [_card("s1", overall=0.5)]),
        )
        regs = detect_regressions(runs, threshold=0.05)
        assert regs
        assert any(r["dimension"] == "overall_score" for r in regs)

    def test_improvement_detected(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", overall=0.5)]),
            _run("r2", "2026-07-02", [_card("s1", overall=1.0)]),
        )
        imps = detect_improvements(runs, threshold=0.05)
        assert imps

    def test_threshold_respected(self):
        # Small drop below threshold → no regression flagged
        runs = (
            _run("r1", "2026-07-01", [_card("s1", overall=1.0)]),
            _run("r2", "2026-07-02", [_card("s1", overall=0.97)]),
        )
        assert detect_regressions(runs, threshold=0.05) == []


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

class TestHeatmap:
    def test_empty_run_empty_heatmap(self):
        h = build_heatmap(_run("r1", "", []))
        assert h["rows"] == []
        assert h["values"] == []

    def test_populated_heatmap_shape(self):
        h = build_heatmap(_run("r1", "", [_card("s1"), _card("s2")]))
        assert h["rows"] == ["s1", "s2"]
        assert len(h["cols"]) == 8
        assert len(h["values"]) == 2
        assert len(h["values"][0]) == 8

    def test_series_sorted_by_generated_at(self):
        runs = (
            _run("b", "2026-07-02", [_card("s1")]),
            _run("a", "2026-07-01", [_card("s1")]),
        )
        s = build_heatmap_series(runs)
        assert s[0]["run_id"] == "a"
        assert s[1]["run_id"] == "b"

    def test_deterministic_json(self):
        run = _run("r1", "", [_card("s2"), _card("s1")])
        # Rows sorted by scenario_id inside build_heatmap
        j1 = json.dumps(build_heatmap(run), sort_keys=True)
        j2 = json.dumps(build_heatmap(run), sort_keys=True)
        assert j1 == j2
