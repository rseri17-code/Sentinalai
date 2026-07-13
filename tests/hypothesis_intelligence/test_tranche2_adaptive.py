"""Tranche 2 — adaptive evidence acquisition advisor tests.

Coverage: uncertainty model, next-best-evidence ranking, stop
conditions, stop-point simulation, contradiction detection, temporal
causality, bounded reclassification, shadow contract, determinism.
"""
from __future__ import annotations

import copy
import json

from supervisor.adaptive_investigation import (
    build_uncertainty_map,
    check_temporal_causality,
    detect_contradictions,
    evaluate_stop,
    recommend_reclassification,
    run_adaptive_advisor,
    select_next_evidence,
    simulate_stop_point,
)


def _meta():
    return [
        {"name": "OOM kill on checkout",
         "root_cause": "container OOMKilled memory limit too low",
         "score": 70.0,
         "evidence_refs": ["search_oom_logs", "check_memory_metrics"]},
        {"name": "bad deployment regression",
         "root_cause": "regression from deploy 4412 rollout",
         "score": 55.0,
         "evidence_refs": ["check_changes", "diff_analysis"]},
    ]


def _evidence(**extra):
    ev = {"search_oom_logs": {"hits": 41},
           "check_memory_metrics": {"rss": "8.1Gi"},
           "check_changes": {"changes": []}}
    ev.update(extra)
    return ev


# ---------------------------------------------------------------------------
# Shadow contract
# ---------------------------------------------------------------------------

class TestShadowContract:
    def test_flag_off_no_op(self, monkeypatch):
        monkeypatch.delenv("ADAPTIVE_INVESTIGATION_ENABLED", raising=False)
        r = {"root_cause": "x", "confidence": 70}
        before = copy.deepcopy(r)
        run_adaptive_advisor(r, _evidence(), "oomkill",
                               hypotheses_meta=_meta())
        assert r == before

    def test_flag_on_additive_only(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_INVESTIGATION_ENABLED", "true")
        r = {"root_cause": "x", "confidence": 70}
        run_adaptive_advisor(r, _evidence(), "oomkill",
                               hypotheses_meta=_meta(),
                               symptom_time="2026-07-10T08:00")
        assert r["root_cause"] == "x"
        assert r["confidence"] == 70
        adv = r["_adaptive_investigation"]
        for key in ("uncertainty_map", "next_best_evidence", "stop",
                     "stop_point_simulation", "contradictions",
                     "temporal_causality", "reclassification"):
            assert key in adv
        assert adv == json.loads(json.dumps(adv))     # JSON-safe

    def test_never_raises_on_garbage(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_INVESTIGATION_ENABLED", "true")
        run_adaptive_advisor({}, {}, "", hypotheses_meta=None)
        run_adaptive_advisor({"x": 1}, {"y": object()}, "zzz",
                               hypotheses_meta=[{"bad": None}])

    def test_deterministic(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_INVESTIGATION_ENABLED", "true")
        outs = []
        for _ in range(2):
            r = {"root_cause": "x", "confidence": 70}
            run_adaptive_advisor(r, _evidence(), "oomkill",
                                   hypotheses_meta=_meta(),
                                   symptom_time="2026-07-10T08:00")
            outs.append(json.dumps(r["_adaptive_investigation"],
                                     sort_keys=True))
        assert outs[0] == outs[1]


# ---------------------------------------------------------------------------
# Phase 2 — uncertainty model
# ---------------------------------------------------------------------------

class TestUncertaintyMap:
    def test_unanswered_questions_per_hypothesis(self):
        u = build_uncertainty_map(_meta(), {"search_oom_logs",
                                              "check_memory_metrics",
                                              "check_changes"})
        # deploy hypothesis still lacks diff_analysis
        assert "diff_analysis" in u["bad deployment regression"]
        # every listed unknown is genuinely missing
        for unknowns in u.values():
            assert "search_oom_logs" not in unknowns

    def test_catalog_relevance_expands_unknowns(self):
        u = build_uncertainty_map(_meta(), set())
        # the OOM hypothesis should pull catalog memory/pod evidence in
        assert len(u["OOM kill on checkout"]) >= 2


# ---------------------------------------------------------------------------
# Phase 3 — next best evidence
# ---------------------------------------------------------------------------

class TestNextBestEvidence:
    def test_ranked_with_discrimination(self):
        u = {"H1": ["logs", "deploy_history"],
              "H2": ["deploy_history"]}
        ranked = select_next_evidence("oomkill", set(), u)
        assert ranked
        top = ranked[0]
        assert top["capability_id"].startswith("cap:")
        assert top["score"] >= top["expected_value"]
        # any acquisition covering deploy_history serves BOTH hypotheses
        both = [r for r in ranked
                if "deploy_history" in r["missing_evidence"]]
        for r in both:
            assert r["discrimination"] >= 2

    def test_nothing_missing_returns_empty_reduction(self):
        # give it every catalog key possible via a huge evidence set
        from supervisor.deterministic_planner.planner_rules import (
            _capability_catalog,
        )
        all_ev = {e for c in _capability_catalog().values()
                  for e in c.typical_evidence_yield}
        assert select_next_evidence("oomkill", all_ev, {}) == []

    def test_deterministic(self):
        u = {"H1": ["logs"]}
        assert select_next_evidence("x", {"a"}, u) \
            == select_next_evidence("x", {"a"}, u)


# ---------------------------------------------------------------------------
# Phase 5 — stop conditions
# ---------------------------------------------------------------------------

class TestStopConditions:
    def test_confidence_threshold(self):
        v = evaluate_stop({"A": 90, "B": 40}, None, 1.0, 10)
        assert v["should_stop"]
        assert "confidence>=85" in v["reasons"]

    def test_margin_threshold(self):
        v = evaluate_stop({"A": 70, "B": 40}, None, 1.0, 10)
        assert "margin>=25" in v["reasons"]

    def test_survival_stops(self):
        v = evaluate_stop({"A": 60, "B": 55}, True, 1.0, 10)
        assert "winner_survived_disconfirmation" in v["reasons"]

    def test_low_remaining_gain_stops(self):
        v = evaluate_stop({"A": 60, "B": 55}, None, 0.0, 10)
        assert "remaining_information_gain_below_cost" in v["reasons"]

    def test_budget_exhaustion_stops(self):
        v = evaluate_stop({"A": 60, "B": 55}, None, 1.0, 0)
        assert "budget_exhausted" in v["reasons"]

    def test_undecided_continues(self):
        v = evaluate_stop({"A": 60, "B": 55}, None, 1.0, 10)
        assert v["should_stop"] is False
        assert v["reasons"] == []


# ---------------------------------------------------------------------------
# Phase 9 — stop-point simulation
# ---------------------------------------------------------------------------

class TestStopPointSimulation:
    def test_early_stop_detected(self):
        # leader's evidence arrives in the first two steps; margin fires
        steps = ["search_oom_logs", "check_memory_metrics",
                  "check_events", "search_memory_logs"]
        sim = simulate_stop_point(_meta(), _evidence(), "oomkill",
                                    steps=steps)
        assert sim["steps_total"] == 4
        assert sim["stop_at_step"] is not None
        assert sim["stop_at_step"] <= 3
        assert sim["unnecessary_calls"] >= 1
        assert sim["estimated_mtti_saving_pct"] > 0

    def test_no_stop_when_evidence_never_decides(self):
        meta = [{"name": "A", "root_cause": "a", "score": 50,
                  "evidence_refs": []},
                 {"name": "B", "root_cause": "b", "score": 50,
                  "evidence_refs": []}]
        sim = simulate_stop_point(meta, {}, "oomkill",
                                    steps=["s1", "s2"])
        # nothing collected, remaining gain 0 → stops on gain-below-cost
        assert sim["stop_at_step"] == 1
        assert "remaining_information_gain_below_cost" in \
            sim["stop_reasons"]

    def test_deterministic(self):
        s1 = simulate_stop_point(_meta(), _evidence(), "oomkill")
        s2 = simulate_stop_point(_meta(), _evidence(), "oomkill")
        assert s1 == s2


# ---------------------------------------------------------------------------
# Phase 6 — contradiction detection
# ---------------------------------------------------------------------------

class TestContradictions:
    def test_error_logs_vs_healthy_golden_signals(self):
        ev = {"search_error_logs": {"hits": 40},
               "check_golden_signals": {"latency_ms": 20}}
        conflicts = detect_contradictions(ev)
        cross = [c for c in conflicts
                 if c["kind"] == "CrossSourceConflict"]
        assert len(cross) == 1
        assert cross[0]["tie_break_recommendation"]
        assert cross[0]["confidence_adjustment"] == -10

    def test_no_conflict_when_sources_agree(self):
        ev = {"search_error_logs": {},
               "check_golden_signals": {"latency_ms": 20}}
        assert not [c for c in detect_contradictions(ev)
                     if c["kind"] == "CrossSourceConflict"]

    def test_errored_source_flagged_not_merged(self):
        ev = {"search_oom_logs": {"error": "timeout", "worker": "log"},
               "check_memory_metrics": {"rss": "8Gi"}}
        conflicts = detect_contradictions(ev)
        rel = [c for c in conflicts
               if c["kind"] == "SourceReliabilityDifferential"]
        assert rel and "search_oom_logs" in rel[0]["sources"]


# ---------------------------------------------------------------------------
# Phase 7 — temporal causality
# ---------------------------------------------------------------------------

class TestTemporalCausality:
    def test_change_after_symptom_demoted(self):
        ev = {"check_changes":
               {"deploys": [{"time": "2026-07-10T09:30:00Z"}]}}
        demotions = check_temporal_causality(
            _meta(), ev, symptom_time="2026-07-10T08:00:00Z")
        assert len(demotions) == 1
        assert demotions[0]["hypothesis"] == "bad deployment regression"
        assert "causal ordering impossible" in demotions[0]["reason"]

    def test_change_before_symptom_allowed(self):
        ev = {"check_changes":
               {"deploys": [{"time": "2026-07-10T07:00:00Z"}]}}
        assert check_temporal_causality(
            _meta(), ev, symptom_time="2026-07-10T08:00:00Z") == []

    def test_non_change_hypotheses_untouched(self):
        ev = {"check_changes":
               {"deploys": [{"time": "2026-07-10T09:30:00Z"}]}}
        demotions = check_temporal_causality(
            _meta(), ev, symptom_time="2026-07-10T08:00:00Z")
        names = [d["hypothesis"] for d in demotions]
        assert "OOM kill on checkout" not in names

    def test_no_symptom_time_no_op(self):
        assert check_temporal_causality(_meta(), {"check_changes": {}},
                                          symptom_time="") == []


# ---------------------------------------------------------------------------
# Phase 8 — bounded reclassification
# ---------------------------------------------------------------------------

class TestReclassification:
    def test_recommends_when_profile_contradicts(self):
        # classified timeout, but ONLY oomkill-signature evidence present
        rec = recommend_reclassification(
            "timeout", {"search_oom_logs", "check_memory_metrics"})
        assert rec is not None
        assert rec["to_type"] == "oomkill"
        assert rec["signature_hits"] == 2
        assert "single reclassification" in rec["bounded"]

    def test_no_recommendation_when_own_signature_present(self):
        assert recommend_reclassification(
            "oomkill", {"search_oom_logs", "check_changes"}) is None

    def test_no_recommendation_without_strong_alternative(self):
        assert recommend_reclassification(
            "timeout", {"check_changes"}) is None
