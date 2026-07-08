"""Sprint 5 regression tests — RC-G + RC-J + RC-L.

The final stabilization sprint. For each RC:

  1. Reproduce the audit-verified defect.
  2. Assert the fixed behavior.
  3. Cover edge cases + compatibility.

Delete this file to fully roll back Sprint 5's test surface.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# RC-G — Identifier correctness
# ---------------------------------------------------------------------------

from sentinel_core.causal_graph.schemas import make_chain_id, make_path_id
from sentinel_core.continuous_learning.learning_cycle import _make_snapshot_id
from sentinel_core.intel_memory.fingerprint import (
    compute_planner_path_hash,
    compute_transaction_path_hash,
)


class TestSnapshotIdNoCollision:

    def test_reproduces_audit_defect_comma_no_longer_collides(self):
        """Audit V-10 — ``["a,b"]`` and ``["a","b"]`` used to hash to
        the same 16-hex id. Sprint 5 fixes this."""
        one = _make_snapshot_id(("a,b",), 0)
        two = _make_snapshot_id(("a", "b"), 0)
        assert one != two

    def test_deterministic_same_input(self):
        a = _make_snapshot_id(("m1", "m2"), 7)
        b = _make_snapshot_id(("m1", "m2"), 7)
        assert a == b
        assert len(a) == 16

    def test_sequence_change_alters_id(self):
        a = _make_snapshot_id(("m1",), 0)
        b = _make_snapshot_id(("m1",), 1)
        assert a != b


class TestFingerprintNoCollision:

    def test_reproduces_audit_defect_transaction_path_gt_no_longer_collides(self):
        """Audit V-12 — a hop containing ``">"`` used to collide with
        the next hop. Sprint 5's framed JSON hash closes the gap."""
        one = compute_transaction_path_hash(("a>b", "c"))
        two = compute_transaction_path_hash(("a", "b", "c"))
        assert one != two

    def test_planner_path_comma_no_longer_collides(self):
        one = compute_planner_path_hash(("cap:a,b",))
        two = compute_planner_path_hash(("cap:a", "b"))
        assert one != two

    def test_deterministic_same_path(self):
        a = compute_transaction_path_hash(("s1", "s2", "s3"))
        b = compute_transaction_path_hash(("s1", "s2", "s3"))
        assert a == b
        assert len(a) == 16

    def test_empty_path_is_stable(self):
        a = compute_transaction_path_hash(())
        b = compute_transaction_path_hash(None)
        # Both should be well-defined and deterministic (equal to each
        # other because ``None`` normalises to ``()``).
        assert a == b


class TestChainIdNoCollision:

    def test_reproduces_audit_defect_comma_no_longer_collides(self):
        one = make_chain_id(("a,b", "c"))
        two = make_chain_id(("a", "b,c"))
        assert one != two

    def test_deterministic_same_input(self):
        a = make_chain_id(("n1", "n2"))
        b = make_chain_id(("n1", "n2"))
        assert a == b
        assert len(a) == 16

    def test_path_id_pipe_no_longer_collides(self):
        one = make_path_id("a|b", "c")
        two = make_path_id("a", "b|c")
        assert one != two


# ---------------------------------------------------------------------------
# RC-J — Data preservation
# ---------------------------------------------------------------------------

from sentinel_core.hypotheses.hypothesis_tracker import HypothesisTracker


class TestHypothesisProposeRefinement:

    def test_reproduces_audit_defect_second_propose_no_longer_silently_dropped(self):
        """Audit V-15 — a second ``propose`` with the same name used to
        return the first Hypothesis unchanged, dropping the refined
        description and higher confidence. Sprint 5's merge policy
        keeps the strictly-more-informative fields."""
        t = HypothesisTracker(investigation_id="inv1")
        first = t.propose("db_pool_exhausted", "", initial_confidence=40)
        second = t.propose(
            "db_pool_exhausted",
            "connections from checkout pool exhausted at 10:03Z",
            initial_confidence=80,
        )
        # Same hypothesis (id is derived from name).
        assert first.hypothesis_id == second.hypothesis_id
        # Refined description survives.
        assert second.description == (
            "connections from checkout pool exhausted at 10:03Z"
        )
        # Confidence upgraded.
        assert second.confidence == 80

    def test_second_propose_never_downgrades_confidence(self):
        t = HypothesisTracker(investigation_id="inv1")
        t.propose("db down", "", initial_confidence=80)
        refined = t.propose("db down", "note", initial_confidence=40)
        assert refined.confidence == 80  # max of 80 and 40

    def test_second_propose_keeps_longer_description(self):
        t = HypothesisTracker(investigation_id="inv1")
        t.propose("h1", "short desc", initial_confidence=50)
        refined = t.propose("h1", "a much longer detailed description",
                              initial_confidence=50)
        assert refined.description == "a much longer detailed description"

    def test_second_propose_does_not_replace_longer_with_shorter(self):
        t = HypothesisTracker(investigation_id="inv1")
        t.propose("h1", "a much longer detailed description",
                   initial_confidence=50)
        refined = t.propose("h1", "short", initial_confidence=50)
        assert refined.description == "a much longer detailed description"

    def test_first_propose_unchanged_when_only_called_once(self):
        t = HypothesisTracker(investigation_id="inv1")
        h = t.propose("hypothesis_a", "orig", initial_confidence=55)
        assert h.description == "orig"
        assert h.confidence == 55

    def test_determinism_regardless_of_call_order_for_two_names(self):
        """Two distinct names in either order → same graph."""
        t_ab = HypothesisTracker(investigation_id="inv1")
        t_ab.propose("a", "d1", 40)
        t_ab.propose("b", "d2", 60)
        t_ba = HypothesisTracker(investigation_id="inv1")
        t_ba.propose("b", "d2", 60)
        t_ba.propose("a", "d1", 40)
        g_ab = t_ab.build_graph()
        g_ba = t_ba.build_graph()
        assert g_ab.to_dict() == g_ba.to_dict()


# ---------------------------------------------------------------------------
# RC-J — IntelligenceContext duplicate module payload merge
# ---------------------------------------------------------------------------

from sentinel_core.models.intel_context import IntelligenceContext


def _receipt(name: str, meta: dict) -> dict:
    return {"metadata": {"intelligence": [{"name": name, "metadata": meta}]}}


class TestIntelContextDuplicateModuleMerge:

    def test_reproduces_audit_defect_duplicate_module_no_longer_drops_data(self):
        """Audit V-18 — before Sprint 3 this was last-write-wins per
        caller order; Sprint 3 made it deterministic-but-still-lossy.
        Sprint 5 merges the payloads so no information is dropped."""
        # Two payloads for historical_lookup, each with its own list of
        # matches. Sprint 5 merges them into one payload containing
        # both matches.
        rA = _receipt("historical_lookup", {
            "resolution_memory_matches": [
                {"memory_id": "mA", "root_cause_head": "rca-a"},
            ]
        })
        rB = _receipt("historical_lookup", {
            "resolution_memory_matches": [
                {"memory_id": "mB", "root_cause_head": "rca-b"},
            ]
        })
        ic = IntelligenceContext.from_receipts([rA, rB])
        # Merged: both memory_ids present.
        memory_ids = {m.memory_id for m in ic.resolution_memory_matches}
        assert memory_ids == {"mA", "mB"}

    def test_scalar_prefers_non_empty(self):
        rA = _receipt("historical_lookup", {"service": "", "incident_type": "T"})
        rB = _receipt("historical_lookup", {"service": "svc", "incident_type": ""})
        ic = IntelligenceContext.from_receipts([rA, rB])
        # Both non-empty values survive somewhere.
        assert ic.service == "svc"
        assert ic.incident_type == "T"

    def test_merge_dedupes_identical_list_entries(self):
        """Same match reported twice → one match in the merged output."""
        rA = _receipt("historical_lookup", {
            "resolution_memory_matches": [
                {"memory_id": "m1", "root_cause_head": "rca"},
            ]
        })
        rB = _receipt("historical_lookup", {
            "resolution_memory_matches": [
                {"memory_id": "m1", "root_cause_head": "rca"},
            ]
        })
        ic = IntelligenceContext.from_receipts([rA, rB])
        assert len(ic.resolution_memory_matches) == 1

    def test_merge_is_deterministic_across_receipt_order(self):
        rA = _receipt("historical_lookup", {
            "resolution_memory_matches": [
                {"memory_id": "mA", "root_cause_head": "rca-a"},
            ]
        })
        rB = _receipt("historical_lookup", {
            "resolution_memory_matches": [
                {"memory_id": "mB", "root_cause_head": "rca-b"},
            ]
        })
        ic_ab = IntelligenceContext.from_receipts([rA, rB])
        ic_ba = IntelligenceContext.from_receipts([rB, rA])
        assert ic_ab.to_dict() == ic_ba.to_dict()

    def test_single_receipt_unchanged(self):
        """Regression — the merge policy must not disturb the single
        -payload case."""
        r = _receipt("historical_lookup", {
            "service": "checkout",
            "incident_type": "latency_spike",
        })
        ic = IntelligenceContext.from_receipts([r])
        assert ic.service == "checkout"
        assert ic.incident_type == "latency_spike"


# ---------------------------------------------------------------------------
# RC-L — Benchmark integrity
# ---------------------------------------------------------------------------

from tests.synthetic.scoring import (
    ScoreCard,
    score_decision_trace_quality,
    score_evidence_completeness,
    score_investigation,
)
from tests.synthetic.schemas import Scenario


def _minimal_scenario(**overrides) -> Scenario:
    defaults = dict(
        scenario_id="s1",
        title="t",
        incident_input={},
        expected_root_cause="database pool exhausted",
        required_evidence=("pod_lifecycle", "logs"),
        red_herrings=(),
        expected_confidence_range=(70, 90),
        expected_decision_signals=("checked_deployment",),
        expected_runtime_cost_budget=20,
        expected_mtti_budget_ms=60_000,
        mocked_evidence_sources={},
        mock_investigation_output={},
    )
    defaults.update(overrides)
    return Scenario(**defaults)


class TestBenchmarkNotMeasuredSignal:

    def test_reproduces_audit_defect_evidence_empty_now_none(self):
        """Audit V-26 — empty required-evidence returned 1.0. Fix: None."""
        assert score_evidence_completeness((), ("anything",)) is None

    def test_reproduces_audit_defect_decision_signals_empty_now_none(self):
        """Audit V-27 — empty expected-signals returned 1.0. Fix: None."""
        assert score_decision_trace_quality((), ("anything",)) is None

    def test_populated_ground_truth_still_returns_float(self):
        s1 = score_evidence_completeness(("a",), ("a",))
        s2 = score_decision_trace_quality(("s1",), ("s1",))
        assert s1 == 1.0
        assert s2 == 1.0

    def test_score_investigation_records_not_measured(self):
        """score_investigation flags NOT-MEASURED dimensions."""
        scenario = _minimal_scenario(
            required_evidence=(),           # → not measured
            expected_decision_signals=(),   # → not measured
        )
        io = {
            "root_cause": "database pool exhausted",
            "evidence_keys": ("anything",),
            "decision_signals": ("anything",),
            "confidence": 80,
            "runtime_cost": 5,
            "mtti_ms": 40_000,
        }
        card = score_investigation(scenario, io)
        assert card.evidence_completeness is None
        assert card.decision_trace_quality is None
        assert set(card.not_measured) == {
            "evidence_completeness", "decision_trace_quality"
        }

    def test_overall_score_ignores_not_measured_dimensions(self):
        """When two dimensions are NOT MEASURED, overall renormalises
        the remaining weights and does not inflate the score."""
        scenario = _minimal_scenario(
            required_evidence=(),
            expected_decision_signals=(),
        )
        # investigation reports nothing right except the RCA
        io = {
            "root_cause": "database pool exhausted",  # matches → 1.0
            "evidence_keys": (),
            "decision_signals": (),
            "confidence": 200,   # out of expected range → 0.0
            "runtime_cost": 999,  # way over budget → clamped down
            "mtti_ms": 999_999,   # way over budget → clamped down
        }
        card = score_investigation(scenario, io)
        # Both scoring dimensions with empty ground truth are skipped;
        # overall is a weighted average of the remaining five.
        assert card.overall_score < 1.0
        # And crucially not inflated by the two skipped 1.0s.
        assert card.evidence_completeness is None
        assert card.decision_trace_quality is None

    def test_to_dict_serialises_not_measured_field(self):
        card = ScoreCard(scenario_id="s", not_measured=("evidence_completeness",))
        d = card.to_dict()
        assert d["not_measured"] == ["evidence_completeness"]

    def test_to_dict_serialises_none_score_as_null(self):
        card = ScoreCard(scenario_id="s", evidence_completeness=None)
        d = card.to_dict()
        j = json.loads(json.dumps(d))
        assert j["evidence_completeness"] is None

    def test_fully_populated_scenario_unchanged_semantics(self):
        """Regression — scenarios with non-empty ground truth score
        identically to pre-Sprint 5 (except overall now uses
        renormalisation, which is a no-op when no dimension is
        skipped)."""
        scenario = _minimal_scenario()
        card = score_investigation(scenario)   # mock output
        for dim in (card.root_cause_match, card.evidence_completeness,
                    card.red_herring_resistance, card.confidence_calibration,
                    card.decision_trace_quality, card.runtime_cost_score,
                    card.mtti_score):
            assert dim is None or isinstance(dim, float)
        assert card.not_measured == ()
