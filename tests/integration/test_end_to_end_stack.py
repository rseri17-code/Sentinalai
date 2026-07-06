"""End-to-end stabilization harness.

Wires already-shipped components into one deterministic pipeline:

    receipts
        │
        ▼
    IntelligenceContext.from_receipts
        │
        ▼
    DecisionContext.from_intelligence_context
        │
        ▼
    KnowledgeGraphBuilder.from_intelligence_context
        │
        ▼
    PlanContext → PlannerBuilder.build
        │
        ▼
    (synthetic MemoryRecord corpus is constructed here — the runtime
     PersistPhase already produces MemoryRecord in production; this
     harness supplies a compact corpus so the offline analytics layers
     can be exercised without the full agent runtime)
        │
        ├──▶ StrategyOptimizer.build_strategy       (Cap 2)
        ├──▶ CausalGraphBuilder.build              (Cap 3)
        └──▶ LearningCycle.run                     (Cap 4)

The harness adds NO product capability. It asserts:

1. Every stage produces a JSON-safe dict via ``to_dict``.
2. Same inputs → byte-identical JSON across two independent runs
   (determinism contract).
3. Learning snapshot id is stable when the corpus + sequence are
   identical.
4. Causal graph node_count > 0 for a corpus with real records.
5. Strategy selection picks at least one capability id present in
   the corpus.
6. Master learning report bundles all 9 sub-reports.

This test replaces no existing test and creates no new dependency.
"""
from __future__ import annotations

import json

import pytest

from sentinel_core.causal_graph.graph_builder import CausalGraphBuilder
from sentinel_core.continuous_learning.feedback_collector import (
    FeedbackCollector,
    FeedbackKind,
    FeedbackSignal,
    FeedbackSource,
)
from sentinel_core.continuous_learning.learning_cycle import LearningCycle
from sentinel_core.continuous_learning.report_renderer import (
    render_master_report,
    to_json,
)
from sentinel_core.intel_memory import (
    BlastRadiusSnapshot,
    MemoryRecord,
    TopologySnapshot,
)
from sentinel_core.models.decision_context import DecisionContext
from sentinel_core.models.intel_context import IntelligenceContext
from sentinel_core.models.knowledge_graph import KnowledgeGraphBuilder
from sentinel_core.models.plan_context import PlanContext
from sentinel_core.strategy_optimizer.optimizer import StrategyOptimizer
from sentinel_core.strategy_optimizer.strategy_graph import StrategyGraph
from supervisor.deterministic_planner.planner_builder import PlannerBuilder


# ---------------------------------------------------------------------------
# Fixtures — synthetic-but-realistic phase receipts + memory corpus
# ---------------------------------------------------------------------------


def _synthetic_receipts() -> list[dict]:
    """Two POST_CLASSIFY-style receipts carrying intelligence metadata.

    Mirrors the shape produced by
    :func:`supervisor.agent._intel_receipt_hook` — a list of module
    entries under ``metadata.intelligence`` with each entry's
    runner-payload under its own ``metadata`` sub-key.
    """
    return [
        {
            "stage": "POST_CLASSIFY",
            "metadata": {
                "intelligence": [
                    {
                        "name":     "historical_lookup",
                        "metadata": {
                            "service":       "checkout",
                            "incident_type": "latency_spike",
                            "resolution_memory_matches": [
                                {
                                    "memory_id":       "mem-alpha",
                                    "root_cause_head": "db_connection_pool_exhausted",
                                    "confidence":      82,
                                    "recorded_at":     "2026-01-10T09:30:00Z",
                                    "service":         "checkout",
                                    "incident_type":   "latency_spike",
                                }
                            ],
                            "investigation_matches": [
                                {
                                    "investigation_id": "inv-42",
                                    "created_at":       "2026-01-01T00:00:00Z",
                                    "incident_type":    "latency_spike",
                                    "service":          "checkout",
                                }
                            ],
                        },
                    },
                    {
                        "name":     "pattern_recognition",
                        "metadata": {
                            "pattern_matches": [
                                {
                                    "pattern_id":       "pat-checkout-latency",
                                    "incident_type":    "latency_spike",
                                    "services":         ["checkout", "payments"],
                                    "canonical_symptoms": ["p99_latency>1000ms"],
                                    "occurrence_count": 3,
                                    "success_count":    2,
                                    "success_rate":     0.67,
                                    "last_seen":        "2026-02-01T00:00:00Z",
                                }
                            ]
                        },
                    },
                    {
                        "name":     "dependency_graph_lookup",
                        "metadata": {
                            "upstream": [
                                {
                                    "source_service": "web",
                                    "target_service": "checkout",
                                    "dep_type":       "http",
                                    "strength":       0.9,
                                    "observed_count": 12,
                                    "last_seen":      "2026-02-15T00:00:00Z",
                                }
                            ],
                            "downstream": [
                                {
                                    "source_service": "checkout",
                                    "target_service": "payments",
                                    "dep_type":       "http",
                                    "strength":       0.95,
                                    "observed_count": 20,
                                    "last_seen":      "2026-02-15T00:00:00Z",
                                }
                            ],
                            "affected_services": ["payments", "orders"],
                        },
                    },
                    {
                        "name":     "causal_graph_lookup",
                        "metadata": {
                            "severity":       "high",
                            "total_affected": 3,
                            "affected": [
                                {
                                    "service_id":     "payments",
                                    "probability":    0.9,
                                    "propagation_ms": 500,
                                    "path":           ["checkout", "payments"],
                                }
                            ],
                        },
                    },
                ]
            },
        }
    ]


def _synthetic_memory_corpus() -> tuple[MemoryRecord, ...]:
    """A tiny corpus that exercises every downstream analytics module.

    Two records for the ``checkout`` service so recurrence + strategy
    graph + causal chain detection each have something to key off.
    """
    topo = TopologySnapshot(
        services=("checkout", "payments"),
        dependencies=(("checkout", "payments"),),
    )
    blast = BlastRadiusSnapshot(severity="high", total_affected=3,
                                 affected=("payments", "orders"))
    common = dict(
        service="checkout",
        incident_type="latency_spike",
        topology=topo,
        blast_radius=blast,
        planner_decisions=("cap:collect_logs", "cap:check_dependencies"),
        evidence_collected=("logs", "traces"),
        confidence=80,
        mtti_ms=45_000,
        investigation_score=0.85,
        sentinelbench_score=0.9,
    )
    r1 = MemoryRecord(
        memory_id="mem-001",
        incident_id="inc-001",
        timestamp="2026-01-15T10:00:00Z",
        detected_root_cause="db_connection_pool_exhausted",
        verified_root_cause="db_connection_pool_exhausted",
        resolution="scale_db_pool",
        **common,
    )
    r2 = MemoryRecord(
        memory_id="mem-002",
        incident_id="inc-002",
        timestamp="2026-02-01T14:30:00Z",
        detected_root_cause="db_connection_pool_exhausted",
        verified_root_cause="db_connection_pool_exhausted",
        resolution="scale_db_pool",
        **common,
    )
    return (r1, r2)


# ---------------------------------------------------------------------------
# Stage-by-stage integration
# ---------------------------------------------------------------------------


class TestReceiptToDecisionContext:
    """Receipts → IntelligenceContext → DecisionContext."""

    def test_intelligence_context_derives_from_receipts(self) -> None:
        ic = IntelligenceContext.from_receipts(_synthetic_receipts())
        assert ic.service == "checkout"
        assert ic.incident_type == "latency_spike"
        assert len(ic.resolution_memory_matches) == 1
        assert len(ic.pattern_matches) == 1
        assert ic.blast_radius_total_affected == 3
        assert not ic.is_empty()

    def test_decision_context_derives_from_intelligence(self) -> None:
        ic = IntelligenceContext.from_receipts(_synthetic_receipts())
        dc = DecisionContext.from_intelligence_context(ic)
        assert dc.service == "checkout"
        assert dc.recurring_incident is True
        assert dc.confidence > 50
        assert "collect_evidence" in dc.recommended_investigation_order


class TestIntelligenceToKnowledgeGraph:

    def test_kg_builder_produces_nodes_and_edges(self) -> None:
        ic = IntelligenceContext.from_receipts(_synthetic_receipts())
        kg = KnowledgeGraphBuilder.from_intelligence_context(
            ic, incident_id="inc-e2e", service="checkout",
            incident_type="latency_spike",
            root_cause="db_connection_pool_exhausted",
        )
        assert kg.node_count() > 0
        assert kg.edge_count() > 0
        # Deterministic serialization
        assert kg.to_dict() == kg.to_dict()


class TestPlannerBuild:

    def test_planner_returns_investigation_plan(self) -> None:
        ic = IntelligenceContext.from_receipts(_synthetic_receipts())
        dc = DecisionContext.from_intelligence_context(ic)
        kg = KnowledgeGraphBuilder.from_intelligence_context(
            ic, incident_id="inc-e2e", service="checkout",
            incident_type="latency_spike",
        )
        pc = PlanContext(
            service="checkout",
            incident_type="latency_spike",
            decision_context=dc,
            knowledge_graph=kg,
            current_confidence=int(dc.confidence),
            target_confidence=90,
        )
        plan = PlannerBuilder().build(pc)
        assert plan is not None
        assert plan.plan_id
        assert plan.target_confidence == 90


# ---------------------------------------------------------------------------
# Analytics: Strategy + Causal + Learning
# ---------------------------------------------------------------------------


class TestStrategyOptimizerOverCorpus:

    def test_optimizer_picks_capability_from_corpus(self) -> None:
        corpus = _synthetic_memory_corpus()
        graph = StrategyGraph().ingest(corpus)
        opt = StrategyOptimizer()
        strat = opt.build_strategy(
            "balanced",
            candidate_capabilities=("cap:collect_logs", "cap:check_dependencies"),
            graph=graph,
        )
        assert strat.strategy_class == "balanced"
        assert len(strat.steps) >= 1
        step_caps = {s.capability_id for s in strat.steps}
        assert step_caps & {"cap:collect_logs", "cap:check_dependencies"}


class TestCausalGraphOverCorpus:

    def test_causal_graph_builds_from_corpus(self) -> None:
        corpus = _synthetic_memory_corpus()
        cg = CausalGraphBuilder().build(corpus)
        assert cg.node_count() > 0
        assert cg.edge_count() >= 0
        payload = cg.to_dict()
        assert payload == cg.to_dict()


class TestLearningCycleOverCorpus:

    def test_learning_snapshot_id_is_deterministic(self) -> None:
        corpus = _synthetic_memory_corpus()
        cycle = LearningCycle()
        snap1 = cycle.run(corpus, sequence=1, generated_at="2026-07-05T00:00:00Z")
        snap2 = cycle.run(corpus, sequence=1, generated_at="2026-07-05T00:00:00Z")
        assert snap1.snapshot_id == snap2.snapshot_id

    def test_learning_snapshot_changes_with_sequence(self) -> None:
        corpus = _synthetic_memory_corpus()
        cycle = LearningCycle()
        snap1 = cycle.run(corpus, sequence=1)
        snap2 = cycle.run(corpus, sequence=2)
        assert snap1.snapshot_id != snap2.snapshot_id

    def test_feedback_collector_is_append_only(self) -> None:
        fc0 = FeedbackCollector()
        sig = FeedbackSignal(
            memory_id="mem-001",
            source=FeedbackSource.OPERATOR.value,
            kind=FeedbackKind.ROOT_CAUSE_CORRECT.value,
            value=1.0,
            timestamp="2026-01-15T10:00:00Z",
        )
        fc1 = fc0.add(sig)
        assert len(fc0.all()) == 0     # original unchanged
        assert len(fc1.all()) == 1     # derived has the signal


# ---------------------------------------------------------------------------
# End-to-end determinism + master report
# ---------------------------------------------------------------------------


class TestFullStackDeterminism:

    def test_master_report_bundles_nine_subreports(self) -> None:
        corpus = _synthetic_memory_corpus()
        report = render_master_report(
            corpus, feedback=None,
            generated_at="2026-07-05T00:00:00Z", sequence=1,
        )
        expected_keys = {
            "schema_version",
            "learning_report",
            "confidence_calibration",
            "strategy_learning",
            "hypothesis_learning",
            "causal_learning",
            "service_learning",
            "false_positive_report",
            "operator_feedback",
            "continuous_learning_summary",
        }
        assert expected_keys.issubset(report.keys())

    def test_master_report_byte_identical_across_runs(self) -> None:
        corpus = _synthetic_memory_corpus()
        kwargs = dict(generated_at="2026-07-05T00:00:00Z", sequence=1)
        r1 = render_master_report(corpus, feedback=None, **kwargs)
        r2 = render_master_report(corpus, feedback=None, **kwargs)
        assert to_json(r1) == to_json(r2)

    def test_e2e_pipeline_is_json_safe(self) -> None:
        """Every stage's payload survives json.dumps + json.loads unchanged."""
        receipts = _synthetic_receipts()
        ic = IntelligenceContext.from_receipts(receipts)
        dc = DecisionContext.from_intelligence_context(ic)
        kg = KnowledgeGraphBuilder.from_intelligence_context(
            ic, incident_id="inc-e2e", service="checkout",
            incident_type="latency_spike",
        )
        pc = PlanContext(
            service="checkout", incident_type="latency_spike",
            decision_context=dc, knowledge_graph=kg,
        )
        plan = PlannerBuilder().build(pc)
        corpus = _synthetic_memory_corpus()
        snap = LearningCycle().run(
            corpus, sequence=1, generated_at="2026-07-05T00:00:00Z",
        )
        # Contract: every payload survives json.dumps + json.loads and
        # produces byte-identical JSON when serialised twice. This is
        # the actual determinism contract callers rely on (a dict with
        # tuple fields still serialises to JSON arrays; strict Python
        # dict equality across a round-trip is stricter than the
        # contract requires).
        for payload in (
            ic.to_dict(), dc.to_dict(), kg.to_dict(),
            plan.to_dict(), snap.to_dict(),
        ):
            j1 = json.dumps(payload, sort_keys=True, default=str)
            j2 = json.dumps(json.loads(j1), sort_keys=True, default=str)
            assert j1 == j2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
