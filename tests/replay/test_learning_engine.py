"""SentinelReplay — LearningEngine + RecommendationEngine tests."""
from __future__ import annotations

import pytest

from tests.replay.learning_engine import LearningEngine
from tests.replay.recommendation_engine import RecommendationEngine
from tests.replay.schemas import (
    BenchmarkRun,
    Recommendation,
    RecommendationKind,
    WeaknessRecord,
    WeaknessType,
)
from tests.synthetic.scoring import ScoreCard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card(scenario_id: str, **overrides) -> ScoreCard:
    defaults = {
        "root_cause_match":        1.0,
        "evidence_completeness":   1.0,
        "red_herring_resistance":  1.0,
        "confidence_calibration":  1.0,
        "decision_trace_quality":  1.0,
        "runtime_cost_score":      1.0,
        "mtti_score":              1.0,
        "overall_score":           1.0,
    }
    defaults.update(overrides)
    return ScoreCard(scenario_id=scenario_id, **defaults)


def _run(run_id: str, generated_at: str, cards) -> BenchmarkRun:
    return BenchmarkRun(
        run_id=run_id, generated_at=generated_at,
        scorecards=tuple(cards),
    )


# ---------------------------------------------------------------------------
# LearningEngine
# ---------------------------------------------------------------------------

class TestLearningEngine:
    def test_no_runs_no_weaknesses(self):
        assert LearningEngine().analyze(()) == ()

    def test_all_perfect_no_weaknesses(self):
        runs = (_run("r1", "2026-07-01", [_card("s1")]),)
        assert LearningEngine().analyze(runs) == ()

    def test_two_consecutive_weak_scores_detected(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", evidence_completeness=0.3, overall_score=0.5)]),
            _run("r2", "2026-07-02", [_card("s1", evidence_completeness=0.3, overall_score=0.5)]),
        )
        w = LearningEngine(weak_threshold=0.6, min_consecutive=2).analyze(runs)
        # evidence_completeness triggers a MISSING_EVIDENCE weakness
        types = {rec.weakness_type for rec in w}
        assert WeaknessType.MISSING_EVIDENCE.value in types
        # One dimension (evidence_completeness) recorded
        by_dim = {(rec.scenario_id, rec.dimension) for rec in w}
        assert ("s1", "evidence_completeness") in by_dim

    def test_single_weak_score_not_yet_a_weakness(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", evidence_completeness=0.3)]),
        )
        # min_consecutive=2 → single weak score not enough
        assert LearningEngine(min_consecutive=2).analyze(runs) == ()

    def test_recovery_resets_the_streak(self):
        runs = (
            _run("r1", "2026-07-01", [_card("s1", evidence_completeness=0.3)]),
            _run("r2", "2026-07-02", [_card("s1", evidence_completeness=0.3)]),
            _run("r3", "2026-07-03", [_card("s1", evidence_completeness=1.0)]),
        )
        # After recovery, no tail-weak streak
        assert LearningEngine(min_consecutive=2).analyze(runs) == ()

    def test_deterministic_ordering(self):
        runs = (
            _run("r1", "2026-07-01", [
                _card("s2", evidence_completeness=0.3),
                _card("s1", root_cause_match=0.3),
            ]),
            _run("r2", "2026-07-02", [
                _card("s2", evidence_completeness=0.3),
                _card("s1", root_cause_match=0.3),
            ]),
        )
        w1 = LearningEngine().analyze(runs)
        w2 = LearningEngine().analyze(runs)
        assert [r.to_dict() for r in w1] == [r.to_dict() for r in w2]

    def test_leaderboard_limit(self):
        # Build 3 different scenarios all triggering weaknesses
        cards1 = [_card(s, evidence_completeness=0.3) for s in ("a", "b", "c")]
        cards2 = [_card(s, evidence_completeness=0.3) for s in ("a", "b", "c")]
        runs = (
            _run("r1", "2026-07-01", cards1),
            _run("r2", "2026-07-02", cards2),
        )
        e = LearningEngine()
        w = e.analyze(runs)
        top2 = e.leaderboard(w, limit=2)
        assert len(top2) == 2

    def test_multiple_dimensions_all_weak(self):
        # A scenario that's weak on many dimensions
        weak = _card("s1", root_cause_match=0.2, evidence_completeness=0.2,
                     decision_trace_quality=0.2, confidence_calibration=0.2)
        runs = (
            _run("r1", "2026-07-01", [weak]),
            _run("r2", "2026-07-02", [weak]),
        )
        w = LearningEngine().analyze(runs)
        dims = {rec.dimension for rec in w}
        assert "root_cause_match" in dims
        assert "evidence_completeness" in dims
        assert "decision_trace_quality" in dims
        assert "confidence_calibration" in dims


# ---------------------------------------------------------------------------
# RecommendationEngine
# ---------------------------------------------------------------------------

class TestRecommendationEngine:
    def test_no_weaknesses_no_recommendations(self):
        assert RecommendationEngine().recommend(()) == ()

    def test_missing_evidence_maps_to_collector(self):
        w = (WeaknessRecord(
            weakness_type=WeaknessType.MISSING_EVIDENCE.value,
            scenario_id="s1", dimension="evidence_completeness",
            count=3, average_score=0.2,
        ),)
        recs = RecommendationEngine().recommend(w)
        assert len(recs) == 1
        assert recs[0].kind == RecommendationKind.RECOMMENDED_COLLECTOR.value

    def test_planner_mistake_maps_to_planner_cap(self):
        w = (WeaknessRecord(
            weakness_type=WeaknessType.PLANNER_MISTAKE.value,
            scenario_id="s1", dimension="decision_trace_quality",
            count=2, average_score=0.4,
        ),)
        recs = RecommendationEngine().recommend(w)
        assert recs[0].kind == RecommendationKind.RECOMMENDED_PLANNER_CAP.value

    def test_evidence_field_populated(self):
        w = (WeaknessRecord(
            weakness_type=WeaknessType.MISSING_EVIDENCE.value,
            scenario_id="s1", dimension="evidence_completeness",
            count=3, average_score=0.2,
        ),)
        recs = RecommendationEngine().recommend(w)
        assert recs[0].evidence   # non-empty
        assert any("count=3" in e for e in recs[0].evidence)
        assert any("weakness_type=missing_evidence" in e for e in recs[0].evidence)

    def test_priority_reflects_count_and_score(self):
        low = WeaknessRecord(
            weakness_type=WeaknessType.MISSING_EVIDENCE.value,
            scenario_id="a", dimension="evidence_completeness",
            count=2, average_score=0.5,
        )
        high = WeaknessRecord(
            weakness_type=WeaknessType.MISSING_EVIDENCE.value,
            scenario_id="b", dimension="evidence_completeness",
            count=10, average_score=0.1,
        )
        recs = RecommendationEngine().recommend((low, high))
        # High-count / low-score item should come first
        assert recs[0].related_scenarios == ("b",)
        assert recs[1].related_scenarios == ("a",)

    def test_deterministic(self):
        w = (
            WeaknessRecord(weakness_type="missing_evidence",
                            scenario_id="s2", dimension="evidence_completeness",
                            count=3, average_score=0.2),
            WeaknessRecord(weakness_type="missing_evidence",
                            scenario_id="s1", dimension="evidence_completeness",
                            count=3, average_score=0.2),
        )
        r1 = RecommendationEngine().recommend(w)
        r2 = RecommendationEngine().recommend(w)
        assert [x.to_dict() for x in r1] == [x.to_dict() for x in r2]

    def test_all_weakness_types_have_a_kind_mapping(self):
        for wt in WeaknessType:
            rec = RecommendationEngine().recommend((
                WeaknessRecord(weakness_type=wt.value, scenario_id="s",
                                dimension="evidence_completeness",
                                count=2, average_score=0.5),
            ))
            assert len(rec) == 1
            assert rec[0].kind    # non-empty
