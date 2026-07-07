"""Sprint 3 regression tests — RC-F deterministic aggregation & selection.

Structure:

  1. Unit tests for the new canonical_sort / canonical_top / canonical_max
     helper.
  2. Per-site regression tests: reproduce the audit defect (pre-fix)
     then assert the fix.
  3. Property / permutation tests: shuffle logically-identical inputs
     N times and assert byte-identical outputs.

No existing test is weakened or contradicted. Delete this file to fully
roll back Sprint 3's test surface.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter

import pytest

# ---------------------------------------------------------------------------
# 1. Deterministic helper — unit tests
# ---------------------------------------------------------------------------

from sentinel_core.models._deterministic import (
    canonical_max,
    canonical_sort,
    canonical_top,
)


class TestCanonicalHelpers:

    def test_canonical_sort_primary_only(self):
        items = [("a", 5), ("b", 3), ("c", 8)]
        out = canonical_sort(items, primary=lambda x: -x[1])
        assert [x[0] for x in out] == ["c", "a", "b"]

    def test_canonical_sort_with_secondary(self):
        # Primary tie: (a, 5) and (b, 5) — secondary picks "a" first.
        items = [("b", 5), ("a", 5), ("c", 3)]
        out = canonical_sort(
            items, primary=lambda x: -x[1], secondary=lambda x: x[0],
        )
        assert [x[0] for x in out] == ["a", "b", "c"]

    def test_canonical_top_basic(self):
        c = Counter({"a": 3, "b": 3, "c": 5})
        out = canonical_top(c, 2)
        assert out == [("c", 5), ("a", 3)]

    def test_canonical_top_ties_break_by_key(self):
        """Reproduces the Counter.most_common insertion-order defect."""
        # Insert in reverse-lex order to trigger the Counter surprise.
        c = Counter()
        for k in ("z", "y", "x"):
            c[k] = 3
        # Counter.most_common gives back insertion order for ties → z, y, x.
        # canonical_top must give lex order → x, y, z.
        assert canonical_top(c, 3) == [("x", 3), ("y", 3), ("z", 3)]

    def test_canonical_top_across_permutations(self):
        base = {"a": 5, "b": 5, "c": 3}
        rng = random.Random(42)
        first = canonical_top(Counter(base), 3)
        for _ in range(50):
            items = list(base.items())
            rng.shuffle(items)
            c = Counter(dict(items))
            assert canonical_top(c, 3) == first

    def test_canonical_max_empty_returns_none(self):
        assert canonical_max([], primary=lambda x: x, secondary=lambda x: x) is None

    def test_canonical_max_breaks_ties(self):
        # Same primary (0) — secondary picks "a".
        items = [("b", 0), ("a", 0), ("c", 0)]
        assert canonical_max(
            items, primary=lambda x: x[1], secondary=lambda x: x[0],
        ) == ("a", 0)


# ---------------------------------------------------------------------------
# 2. Per-site regression: Counter.most_common → canonical_top
# ---------------------------------------------------------------------------

from sentinel_core.strategy_optimizer.strategy_graph import StrategyGraph
from sentinel_core.intel_memory import MemoryRecord


def _rec(mid: str, **kw) -> MemoryRecord:
    return MemoryRecord(memory_id=mid, **kw)


class TestStrategyGraphDeterministic:
    """RC-F @ strategy_graph.py — top_capabilities / top_evidence /
    transitions must be stable under Counter tie-break."""

    def _corpus(self):
        # Two capabilities with identical counts — tie test.
        return [
            _rec("m1", planner_decisions=("cap:z", "cap:a")),
            _rec("m2", planner_decisions=("cap:z", "cap:a")),
        ]

    def test_top_capabilities_lex_tiebreak(self):
        g = StrategyGraph().ingest(self._corpus())
        top = g.top_capabilities(limit=2)
        # Both capabilities have count 2 → lex tie-break → "cap:a" first.
        assert top[0][0] == "cap:a"

    def test_top_capabilities_permutation_stable(self):
        rng = random.Random(7)
        base = list(self._corpus())
        first = StrategyGraph().ingest(base).top_capabilities(limit=5)
        for _ in range(30):
            shuffled = list(base)
            rng.shuffle(shuffled)
            got = StrategyGraph().ingest(shuffled).top_capabilities(limit=5)
            assert got == first

    def test_evidence_transitions_tuple_key_tiebreak(self):
        # Two transitions with identical counts — tuple lex must break tie.
        corpus = [
            _rec("m1", evidence_ordering=("logs", "traces")),
            _rec("m2", evidence_ordering=("logs", "metrics")),
        ]
        g = StrategyGraph().ingest(corpus)
        got = g.evidence_transitions(limit=5)
        # Both counts equal → lex tie-break on (from, to) tuple.
        assert got == sorted(got, key=lambda kv: (-kv[1], kv[0]))


# ---------------------------------------------------------------------------
# 3. DecisionContext determinism
# ---------------------------------------------------------------------------

from sentinel_core.models.intel_context import (
    AffectedService,
    DependencyEdge,
    IntelligenceContext,
    PatternMatch,
)
from sentinel_core.models.decision_context import DecisionContext


class TestDecisionContextDeterministic:

    def test_top_service_no_longer_flips_on_input_order(self):
        """Audit V-17: `[A,B]` and `[B,A]` used to give different top_service."""
        ic_ab = IntelligenceContext(
            blast_radius_severity="high",
            blast_radius_total_affected=2,
            blast_radius_affected=(
                AffectedService(service_id="A", probability=0.1, propagation_ms=100),
                AffectedService(service_id="B", probability=0.9, propagation_ms=100),
            ),
        )
        ic_ba = IntelligenceContext(
            blast_radius_severity="high",
            blast_radius_total_affected=2,
            blast_radius_affected=(
                AffectedService(service_id="B", probability=0.9, propagation_ms=100),
                AffectedService(service_id="A", probability=0.1, propagation_ms=100),
            ),
        )
        assert (
            DecisionContext.from_intelligence_context(ic_ab).likely_blast_radius.top_service
            == DecisionContext.from_intelligence_context(ic_ba).likely_blast_radius.top_service
        )

    def test_top_service_selects_highest_probability(self):
        """Docstring says 'highest-probability'. Fix restores that."""
        ic = IntelligenceContext(
            blast_radius_severity="high",
            blast_radius_total_affected=2,
            blast_radius_affected=(
                AffectedService(service_id="A", probability=0.1, propagation_ms=100),
                AffectedService(service_id="B", probability=0.9, propagation_ms=100),
            ),
        )
        dc = DecisionContext.from_intelligence_context(ic)
        assert dc.likely_blast_radius.top_service == "B"

    def test_top_service_tie_lex_breaks(self):
        ic = IntelligenceContext(
            blast_radius_severity="high",
            blast_radius_total_affected=2,
            blast_radius_affected=(
                AffectedService(service_id="Z", probability=0.5, propagation_ms=100),
                AffectedService(service_id="A", probability=0.5, propagation_ms=100),
            ),
        )
        dc = DecisionContext.from_intelligence_context(ic)
        assert dc.likely_blast_radius.top_service == "A"

    def test_recommended_next_service_no_longer_flips_on_ties(self):
        """Audit V-22: equal-strength downstreams used to flip on input order."""
        edge_A = DependencyEdge(source_service="A", target_service="t",
                                  dep_type="http", strength=0.5)
        edge_B = DependencyEdge(source_service="B", target_service="t",
                                  dep_type="http", strength=0.5)
        ic_ab = IntelligenceContext(downstream_dependents=(edge_A, edge_B))
        ic_ba = IntelligenceContext(downstream_dependents=(edge_B, edge_A))
        assert (
            DecisionContext.from_intelligence_context(ic_ab).recommended_next_service
            == DecisionContext.from_intelligence_context(ic_ba).recommended_next_service
        )

    def test_top_pattern_no_longer_flips_on_ties(self):
        """Audit V-24: equal occurrence_count patterns used to flip
        likely_failure_type on input order."""
        p_db = PatternMatch(pattern_id="p_db", incident_type="db_outage",
                            occurrence_count=5)
        p_net = PatternMatch(pattern_id="p_net", incident_type="net_outage",
                              occurrence_count=5)
        ic_ab = IntelligenceContext(pattern_matches=(p_db, p_net))
        ic_ba = IntelligenceContext(pattern_matches=(p_net, p_db))
        assert (
            DecisionContext.from_intelligence_context(ic_ab).likely_failure_type
            == DecisionContext.from_intelligence_context(ic_ba).likely_failure_type
        )


# ---------------------------------------------------------------------------
# 4. IntelligenceContext duplicate module receipts
# ---------------------------------------------------------------------------

class TestIntelContextDuplicateModules:

    def test_duplicate_module_payload_deterministic_across_shuffle(self):
        """Audit V-18: two receipts with the same module name and
        different payloads. Fix: which payload 'wins' depends on
        content, not on caller input order."""
        rA = {"metadata": {"intelligence": [
            {"name": "historical_lookup", "metadata": {"service": "A"}}
        ]}}
        rB = {"metadata": {"intelligence": [
            {"name": "historical_lookup", "metadata": {"service": "B"}}
        ]}}
        ic_ab = IntelligenceContext.from_receipts([rA, rB])
        ic_ba = IntelligenceContext.from_receipts([rB, rA])
        assert ic_ab.service == ic_ba.service


# ---------------------------------------------------------------------------
# 5. OutcomeRecord.to_dict signal serialization
# ---------------------------------------------------------------------------

from sentinel_core.continuous_learning.feedback_collector import (
    FeedbackKind,
    FeedbackSignal,
    FeedbackSource,
)
from sentinel_core.continuous_learning.outcome_memory import OutcomeRecord


class TestOutcomeRecordSerializationOrder:

    def test_signal_order_no_longer_leaks_into_to_dict(self):
        s1 = FeedbackSignal(memory_id="m1",
                              source=FeedbackSource.REPLAY.value,
                              kind=FeedbackKind.ROOT_CAUSE_CORRECT.value,
                              timestamp="2026-01-01T00:00:00Z")
        s2 = FeedbackSignal(memory_id="m1",
                              source=FeedbackSource.REPLAY.value,
                              kind=FeedbackKind.ROOT_CAUSE_INCORRECT.value,
                              timestamp="2026-02-01T00:00:00Z")
        rec_12 = OutcomeRecord(memory_id="m1", feedback_signals=(s1, s2))
        rec_21 = OutcomeRecord(memory_id="m1", feedback_signals=(s2, s1))
        assert rec_12.to_dict() == rec_21.to_dict()


# ---------------------------------------------------------------------------
# 6. CausalGraph builder order-independence
# ---------------------------------------------------------------------------

from sentinel_core.causal_graph.graph_builder import CausalGraphBuilder


class TestCausalGraphBuilderDeterministic:

    def test_same_records_two_orders_produce_identical_graph(self):
        """Audit V-06: order-dependent confidence stored on shared
        ROOT_CAUSE node."""
        r_low = _rec("m1", service="svc",
                     detected_root_cause="db_pool_exhausted",
                     confidence=10)
        r_high = _rec("m2", service="svc",
                      detected_root_cause="db_pool_exhausted",
                      confidence=90)
        g_ab = CausalGraphBuilder().build([r_low, r_high])
        g_ba = CausalGraphBuilder().build([r_high, r_low])
        assert g_ab.to_dict() == g_ba.to_dict()


# ---------------------------------------------------------------------------
# 7. Property / permutation tests — the RC-F contract
# ---------------------------------------------------------------------------

def _json_hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


class TestPropertyPermutation:
    """Shuffle logically-identical inputs and assert byte-identical outputs.

    The RC-F contract in one place. Each function under test is
    exercised across N random permutations; every hash must equal the
    hash produced by the seed permutation.
    """

    N_SHUFFLES = 25  # bounded to keep the suite fast

    def test_causal_graph_hash_stable_across_permutations(self):
        rng = random.Random(11)
        records = [
            _rec("m1", service="checkout",
                 detected_root_cause="db_pool", confidence=80),
            _rec("m2", service="checkout",
                 detected_root_cause="db_pool", confidence=40),
            _rec("m3", service="payments",
                 detected_root_cause="net_partition", confidence=60),
        ]
        first = _json_hash(CausalGraphBuilder().build(records).to_dict())
        for _ in range(self.N_SHUFFLES):
            shuffled = list(records)
            rng.shuffle(shuffled)
            got = _json_hash(CausalGraphBuilder().build(shuffled).to_dict())
            assert got == first

    def test_strategy_graph_top_capabilities_stable(self):
        rng = random.Random(3)
        base = [
            _rec("m1", planner_decisions=("cap:a", "cap:b")),
            _rec("m2", planner_decisions=("cap:b", "cap:c")),
            _rec("m3", planner_decisions=("cap:a", "cap:c")),
        ]
        first = StrategyGraph().ingest(base).top_capabilities(limit=10)
        for _ in range(self.N_SHUFFLES):
            shuffled = list(base)
            rng.shuffle(shuffled)
            got = StrategyGraph().ingest(shuffled).top_capabilities(limit=10)
            assert got == first

    def test_decision_context_hash_stable_across_permutations(self):
        rng = random.Random(19)
        base_patterns = [
            PatternMatch(pattern_id=f"p{i}", incident_type=f"t{i}",
                          occurrence_count=3, success_rate=0.5)
            for i in range(5)
        ]
        base_affected = [
            AffectedService(service_id=f"svc-{c}", probability=0.5,
                             propagation_ms=100)
            for c in "abcde"
        ]
        first = None
        for i in range(self.N_SHUFFLES + 1):
            pats = list(base_patterns); rng.shuffle(pats)
            affected = list(base_affected); rng.shuffle(affected)
            ic = IntelligenceContext(
                pattern_matches=tuple(pats),
                blast_radius_severity="high",
                blast_radius_total_affected=5,
                blast_radius_affected=tuple(affected),
            )
            dc = DecisionContext.from_intelligence_context(ic)
            h = _json_hash(dc.to_dict())
            if first is None:
                first = h
            else:
                assert h == first

    def test_intel_context_receipts_hash_stable(self):
        rng = random.Random(23)
        # Multiple modules; duplicates present. Any shuffle must produce
        # the same IntelligenceContext JSON.
        base = [
            {"metadata": {"intelligence": [
                {"name": "historical_lookup",
                 "metadata": {"service": "svc-x"}},
            ]}},
            {"metadata": {"intelligence": [
                {"name": "pattern_recognition",
                 "metadata": {"pattern_matches": [
                     {"pattern_id": "p1", "incident_type": "a",
                      "occurrence_count": 2, "success_count": 1,
                      "success_rate": 0.5},
                 ]}},
            ]}},
        ]
        first = _json_hash(IntelligenceContext.from_receipts(base).to_dict())
        for _ in range(self.N_SHUFFLES):
            shuffled = list(base)
            rng.shuffle(shuffled)
            got = _json_hash(IntelligenceContext.from_receipts(shuffled).to_dict())
            assert got == first

    def test_outcome_record_hash_stable_across_signal_shuffles(self):
        rng = random.Random(29)
        signals = [
            FeedbackSignal(memory_id=f"m{i}",
                            source=FeedbackSource.REPLAY.value,
                            kind=FeedbackKind.ROOT_CAUSE_CORRECT.value,
                            timestamp=f"2026-01-{i:02d}T00:00:00Z")
            for i in range(1, 6)
        ]
        first = _json_hash(OutcomeRecord(
            memory_id="m", feedback_signals=tuple(signals)).to_dict())
        for _ in range(self.N_SHUFFLES):
            shuffled = list(signals)
            rng.shuffle(shuffled)
            got = _json_hash(OutcomeRecord(
                memory_id="m", feedback_signals=tuple(shuffled)).to_dict())
            assert got == first
