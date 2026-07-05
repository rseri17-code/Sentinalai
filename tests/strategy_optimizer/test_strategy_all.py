"""Strategy Optimizer — comprehensive tests."""
from __future__ import annotations

import json

import pytest

from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.strategy_optimizer import (
    CostModel,
    InvestigationStrategy,
    MttiEstimation,
    MttiEstimator,
    StrategyClass,
    StrategyGraph,
    StrategyOptimizer,
    StrategyRanker,
    StrategyRecommendation,
    StrategyRecommendationEngine,
    StrategyRecommendationKind,
    StrategyStep,
    render_evidence_value_report,
    render_investigation_efficiency,
    render_master_report,
    render_mtti_estimation,
    render_planner_effectiveness,
    render_recommended_strategy,
    render_strategy_report,
    to_json,
)


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

def _rec(mid, **k) -> MemoryRecord:
    d = dict(memory_id=mid)
    d.update(k)
    return MemoryRecord(**d)


def _corpus() -> tuple[MemoryRecord, ...]:
    return (
        _rec("m1", planner_decisions=("cap:collect_pod_lifecycle",
                                        "cap:collect_logs"),
              evidence_collected=("oom_events", "logs"),
              evidence_ordering=("oom_events", "logs"),
              mtti_ms=45000, confidence=85,
              investigation_score=0.9, runtime_cost=15),
        _rec("m2", planner_decisions=("cap:collect_pod_lifecycle",
                                        "cap:collect_logs"),
              evidence_collected=("oom_events", "logs"),
              evidence_ordering=("oom_events", "logs"),
              mtti_ms=60000, confidence=80,
              investigation_score=0.85, runtime_cost=17),
        _rec("m3", planner_decisions=("cap:collect_dns_state",),
              evidence_collected=("dns_records",),
              mtti_ms=90000, confidence=60,
              investigation_score=0.3, runtime_cost=22),
    )


# ---------------------------------------------------------------------------
# schemas + cost_model
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_frozen(self):
        s = StrategyStep(capability_id="cap:x", step_order=0)
        with pytest.raises(Exception):
            s.step_order = 5

    def test_step_to_dict_json_safe(self):
        s = StrategyStep(capability_id="cap:x", step_order=1,
                          evidence=("a", "b"))
        d = s.to_dict()
        assert d["evidence"] == ["a", "b"]
        json.dumps(d)

    def test_recommendation_to_dict(self):
        r = StrategyRecommendation(
            kind=StrategyRecommendationKind.RECOMMENDED_ORDER.value,
            message="test", evidence=("why",), priority=200,
            related_capabilities=("cap:x",),
        )
        d = r.to_dict()
        assert d["priority"] == 200
        assert "cap:x" in d["related_capabilities"]

    def test_strategy_to_dict(self):
        s = InvestigationStrategy(
            strategy_id="sid", strategy_class="fastest",
            name="Fastest", steps=(),
        )
        assert s.to_dict()["step_count"] == 0

    def test_mtti_estimation_json_safe(self):
        e = MttiEstimation(current_mtti_ms=45000, historical_mtti_ms=50000)
        json.dumps(e.to_dict())


class TestCostModel:
    def test_evidence_cost_defaults(self):
        m = CostModel()
        assert m.evidence_cost(("logs",)) == 2500

    def test_tool_cost_defaults(self):
        m = CostModel()
        assert m.tool_cost(("kubectl_logs",)) == 2000

    def test_switching_cost_matches_default_when_different(self):
        m = CostModel()
        assert m.switching_cost("kubectl_logs", "elastic_logs") == 200

    def test_switching_cost_zero_when_same(self):
        m = CostModel()
        assert m.switching_cost("kubectl_logs", "kubectl_logs") == 0

    def test_execution_cost_composes(self):
        m = CostModel()
        c = m.execution_cost(evidence_keys=("logs",),
                              skills=("kubectl_logs",),
                              prev_skill="elastic_logs")
        assert c == 2500 + 2000 + 200

    def test_overall_value_deterministic(self):
        m = CostModel()
        v1 = m.overall_value(0.5, 80, 0.9, 5000)
        v2 = m.overall_value(0.5, 80, 0.9, 5000)
        assert v1 == v2

    def test_overall_value_penalises_cost(self):
        m = CostModel()
        low  = m.overall_value(0.5, 80, 0.9, 1000)
        high = m.overall_value(0.5, 80, 0.9, 15000)
        assert low > high


# ---------------------------------------------------------------------------
# StrategyGraph
# ---------------------------------------------------------------------------

class TestStrategyGraph:
    def test_empty(self):
        g = StrategyGraph()
        assert g.records_seen() == 0
        assert g.top_capabilities() == []

    def test_ingest_counts(self):
        g = StrategyGraph().ingest(_corpus())
        assert g.records_seen() == 3
        assert g.records_success() == 2
        assert g.capability_count("cap:collect_pod_lifecycle") == 2

    def test_capability_success_rate(self):
        g = StrategyGraph().ingest(_corpus())
        # cap:collect_pod_lifecycle used in 2 records both successful
        assert g.capability_success_rate("cap:collect_pod_lifecycle") == 1.0
        # cap:collect_dns_state used in 1 record with score 0.3 → 0.0
        assert g.capability_success_rate("cap:collect_dns_state") == 0.0

    def test_top_evidence_deterministic(self):
        g = StrategyGraph().ingest(_corpus())
        top = g.top_evidence(limit=5)
        # "logs" and "oom_events" both appear twice
        keys = {t[0] for t in top}
        assert "oom_events" in keys
        assert "logs" in keys

    def test_transitions(self):
        g = StrategyGraph().ingest(_corpus())
        # oom_events → logs seen twice
        pairs = dict(g.evidence_transitions(limit=5))
        assert pairs[("oom_events", "logs")] == 2


# ---------------------------------------------------------------------------
# MttiEstimator
# ---------------------------------------------------------------------------

class TestMttiEstimator:
    def test_empty_corpus(self):
        e = MttiEstimator().estimate((), current_mtti_ms=50000)
        assert e.historical_mtti_ms == 0
        assert e.expected_mtti_ms == 50000
        assert e.sample_size == 0

    def test_historical_mean(self):
        e = MttiEstimator().estimate(_corpus(), current_mtti_ms=70000)
        # historical mean = (45k+60k+90k) / 3 = 65_000
        assert e.historical_mtti_ms == 65000
        assert e.sample_size == 3

    def test_improvement_positive(self):
        e = MttiEstimator().estimate(_corpus(), current_mtti_ms=100000)
        # expected = min(65000, 100000) = 65000; improvement = 35000
        assert e.potential_improvement_ms == 35000
        assert e.potential_improvement_pct > 0

    def test_confidence_interval_shape(self):
        e = MttiEstimator().estimate(_corpus(), current_mtti_ms=65000)
        lo, hi = e.confidence_interval
        assert lo >= 0
        assert hi >= lo


# ---------------------------------------------------------------------------
# StrategyOptimizer
# ---------------------------------------------------------------------------

class TestStrategyOptimizer:
    def test_evaluate_step_deterministic(self):
        g = StrategyGraph().ingest(_corpus())
        o = StrategyOptimizer()
        s1 = o.evaluate_step("cap:collect_pod_lifecycle", g)
        s2 = o.evaluate_step("cap:collect_pod_lifecycle", g)
        assert s1 == s2

    def test_build_strategy_orders_deterministically(self):
        g = StrategyGraph().ingest(_corpus())
        caps = ("cap:collect_pod_lifecycle", "cap:collect_dns_state",
                  "cap:collect_logs")
        s = StrategyOptimizer().build_strategy("balanced", caps, g)
        # Steps sorted by overall value; the two high-success
        # capabilities rank ahead of the 0-success one.
        ids = [x.capability_id for x in s.steps]
        top2 = set(ids[:2])
        assert top2 == {"cap:collect_pod_lifecycle", "cap:collect_logs"}
        assert ids[-1] == "cap:collect_dns_state"

    def test_build_all_strategies_six_classes(self):
        g = StrategyGraph().ingest(_corpus())
        caps = ("cap:collect_pod_lifecycle", "cap:collect_logs")
        strategies = StrategyOptimizer().build_all_strategies(caps, g)
        classes = {s.strategy_class for s in strategies}
        assert classes == {"best", "fastest", "highest_confidence",
                             "lowest_cost", "highest_success", "balanced"}

    def test_strategy_id_deterministic(self):
        g = StrategyGraph().ingest(_corpus())
        caps = ("cap:collect_pod_lifecycle",)
        s1 = StrategyOptimizer().build_strategy("balanced", caps, g)
        s2 = StrategyOptimizer().build_strategy("balanced", caps, g)
        assert s1.strategy_id == s2.strategy_id


# ---------------------------------------------------------------------------
# StrategyRanker
# ---------------------------------------------------------------------------

class TestStrategyRanker:
    def test_ranker_sorts_by_overall_value_desc(self):
        s_low  = InvestigationStrategy(
            strategy_id="a", strategy_class="fastest", name="A",
            overall_value=0.4,
        )
        s_high = InvestigationStrategy(
            strategy_id="b", strategy_class="fastest", name="B",
            overall_value=0.9,
        )
        ranked = StrategyRanker().rank((s_low, s_high))
        assert ranked[0].strategy_id == "b"


# ---------------------------------------------------------------------------
# StrategyRecommendationEngine
# ---------------------------------------------------------------------------

class TestRecommendationEngine:
    def test_empty_corpus_no_recommendations(self):
        r = StrategyRecommendationEngine().recommend(())
        assert r == ()

    def test_recommended_order_from_transitions(self):
        r = StrategyRecommendationEngine().recommend(_corpus())
        # We expect "Collect oom_events before logs" to appear
        assert any("oom_events" in x.message and "logs" in x.message for x in r)

    def test_recommendations_have_evidence(self):
        r = StrategyRecommendationEngine().recommend(_corpus())
        assert r
        for rec in r:
            assert len(rec.evidence) > 0

    def test_prefer_capability_for_high_success(self):
        r = StrategyRecommendationEngine().recommend(_corpus())
        kinds = {x.kind for x in r}
        assert StrategyRecommendationKind.PREFER_CAPABILITY.value in kinds

    def test_deterministic(self):
        r1 = StrategyRecommendationEngine().recommend(_corpus())
        r2 = StrategyRecommendationEngine().recommend(_corpus())
        assert [x.to_dict() for x in r1] == [x.to_dict() for x in r2]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestReports:
    def _strategies(self):
        g = StrategyGraph().ingest(_corpus())
        caps = ("cap:collect_pod_lifecycle", "cap:collect_logs")
        return StrategyOptimizer().build_all_strategies(caps, g)

    def test_strategy_report(self):
        r = render_strategy_report(self._strategies())
        assert r["strategy_count"] == 6

    def test_recommended_strategy_report(self):
        r = render_recommended_strategy(self._strategies())
        assert r["recommended"]

    def test_mtti_estimation_report(self):
        r = render_mtti_estimation(_corpus(), current_mtti_ms=70000)
        assert r["estimation"]["sample_size"] == 3

    def test_planner_effectiveness(self):
        r = render_planner_effectiveness(_corpus())
        assert r["records_seen"] == 3
        assert any(row["capability_id"] == "cap:collect_pod_lifecycle"
                     for row in r["per_capability"])

    def test_evidence_value_report(self):
        r = render_evidence_value_report(_corpus())
        assert r["records_seen"] == 3

    def test_investigation_efficiency(self):
        r = render_investigation_efficiency(_corpus())
        assert len(r["per_record"]) == 3

    def test_master_report_deterministic(self):
        j1 = to_json(render_master_report(_corpus(),
                                             candidate_capabilities=("cap:collect_pod_lifecycle",)))
        j2 = to_json(render_master_report(_corpus(),
                                             candidate_capabilities=("cap:collect_pod_lifecycle",)))
        assert j1 == j2
        # And parses back cleanly
        json.loads(j1)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_no_forbidden_imports(self):
        import importlib
        for name in ("sentinel_core.strategy_optimizer.schemas",
                      "sentinel_core.strategy_optimizer.cost_model",
                      "sentinel_core.strategy_optimizer.mtti_estimator",
                      "sentinel_core.strategy_optimizer.optimizer",
                      "sentinel_core.strategy_optimizer.strategy_graph",
                      "sentinel_core.strategy_optimizer.ranking",
                      "sentinel_core.strategy_optimizer.recommendation_engine",
                      "sentinel_core.strategy_optimizer.report"):
            src = open(importlib.import_module(name).__file__).read()
            for banned in ("requests", "httpx", "urllib3", "boto3",
                             "openai", "anthropic", "supervisor.agent",
                             "kubernetes"):
                assert banned not in src, f"{name} imports {banned}"
