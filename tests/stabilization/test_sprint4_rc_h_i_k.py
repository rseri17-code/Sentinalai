"""Sprint 4 regression tests — RC-H + RC-I + RC-K.

For every RC we ship:
  1. Unit tests for the fix in isolation.
  2. Regression test reproducing the pre-fix defect.
  3. Regression test asserting the fixed behavior.
  4. Compatibility / edge-case tests.

No existing assertion elsewhere in the suite is weakened. Delete this
file to roll back Sprint 4's test surface.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# RC-H — Coercion helper unit tests
# ---------------------------------------------------------------------------

from sentinel_core.models._coerce import (
    coerce_float,
    coerce_int,
    coerce_seq,
    coerce_str,
)


class TestCoerceStr:
    def test_none_returns_empty_string_not_literal_None(self):
        assert coerce_str(None) == ""
        # Explicit check: the RC-H "str(None)" contamination is closed.
        assert coerce_str(None) != "None"

    def test_none_with_custom_default(self):
        assert coerce_str(None, default="fallback") == "fallback"

    def test_pass_through_for_str(self):
        assert coerce_str("abc") == "abc"

    def test_int_becomes_str(self):
        assert coerce_str(42) == "42"


class TestCoerceInt:
    def test_valid_int_string(self):
        assert coerce_int("42") == 42

    def test_valid_int(self):
        assert coerce_int(42) == 42

    def test_none_returns_default(self):
        assert coerce_int(None) == 0
        assert coerce_int(None, default=-1) == -1

    def test_adversarial_string_returns_default(self):
        # RC-H audit case: "N/A" used to raise ValueError from int()
        assert coerce_int("N/A") == 0

    def test_high_returns_default(self):
        assert coerce_int("high") == 0

    def test_float_string_via_fallthrough(self):
        assert coerce_int("42.7") == 42

    def test_float_input(self):
        assert coerce_int(3.9) == 3


class TestCoerceFloat:
    def test_valid_float(self):
        assert coerce_float(3.14) == 3.14

    def test_valid_string(self):
        assert coerce_float("3.14") == 3.14

    def test_none_returns_default(self):
        assert coerce_float(None) == 0.0
        assert coerce_float(None, default=99.0) == 99.0

    def test_adversarial_string(self):
        assert coerce_float("bad") == 0.0

    def test_high_string(self):
        assert coerce_float("high") == 0.0


class TestCoerceSeq:
    def test_none_returns_empty_tuple(self):
        assert coerce_seq(None) == ()

    def test_string_is_scalar_not_iterated(self):
        """RC-H bench-scorer fix: 'abc' → ('abc',), NOT ('a','b','c')."""
        assert coerce_seq("abc") == ("abc",)

    def test_list_passthrough(self):
        assert coerce_seq([1, 2, 3]) == (1, 2, 3)

    def test_tuple_passthrough(self):
        assert coerce_seq((1, 2)) == (1, 2)

    def test_set_becomes_tuple(self):
        assert set(coerce_seq({1, 2, 3})) == {1, 2, 3}

    def test_dict_becomes_items_tuple(self):
        result = coerce_seq({"a": 1})
        assert result == (("a", 1),)


# ---------------------------------------------------------------------------
# RC-H — IntelligenceContext.from_receipts ingest tolerance
# ---------------------------------------------------------------------------

from sentinel_core.models.intel_context import IntelligenceContext


class TestIntelContextIngestTolerance:

    def test_reproduces_null_becomes_none_defect_now_fixed(self):
        """PRE-FIX: JSON null → literal 'None' propagated to service/type.
        POST-FIX: empty string default."""
        receipts = [{"metadata": {"intelligence": [
            {"name": "historical_lookup",
             "metadata": {"service": None, "incident_type": None}}
        ]}}]
        ic = IntelligenceContext.from_receipts(receipts)
        assert ic.service == ""
        assert ic.incident_type == ""
        # Explicit: not literal "None"
        assert ic.service != "None"

    def test_reproduces_int_na_defect_now_fixed(self):
        """PRE-FIX: 'N/A' confidence raised ValueError, aborting
        from_receipts entirely. POST-FIX: coerced to 0 (default)."""
        receipts = [{"metadata": {"intelligence": [
            {"name": "historical_lookup", "metadata": {
                "resolution_memory_matches": [{
                    "memory_id": "m1",
                    "root_cause_head": "rca",
                    "confidence": "N/A",
                }]
            }}
        ]}}]
        # Must not raise
        ic = IntelligenceContext.from_receipts(receipts)
        assert len(ic.resolution_memory_matches) == 1
        assert ic.resolution_memory_matches[0].confidence == 0

    def test_float_adversarial_input_tolerated(self):
        receipts = [{"metadata": {"intelligence": [
            {"name": "causal_graph_lookup", "metadata": {
                "severity": "high",
                "total_affected": "not-a-number",
                "affected": [{
                    "service_id": "svc-a",
                    "probability": "high",
                    "propagation_ms": None,
                    "path": [],
                }],
            }}
        ]}}]
        ic = IntelligenceContext.from_receipts(receipts)
        assert ic.blast_radius_total_affected == 0
        assert ic.blast_radius_affected[0].probability == 0.0
        assert ic.blast_radius_affected[0].propagation_ms == 0

    def test_benign_inputs_still_work(self):
        """Regression: valid inputs still parse identically."""
        receipts = [{"metadata": {"intelligence": [
            {"name": "historical_lookup",
             "metadata": {"service": "checkout",
                          "incident_type": "latency_spike"}}
        ]}}]
        ic = IntelligenceContext.from_receipts(receipts)
        assert ic.service == "checkout"
        assert ic.incident_type == "latency_spike"


# ---------------------------------------------------------------------------
# RC-I — Contract correctness
# ---------------------------------------------------------------------------

from sentinel_core.intel_memory.schemas import MemoryRecord, MEMORY_SCHEMA_VERSION


class TestMemoryRecordSchemaVersionRoundTrip:

    def test_reproduces_audit_defect_schema_version_no_longer_downgraded(self):
        """PRE-FIX: from_dict returned schema_version=1 regardless of
        input. POST-FIX: caller-supplied version preserved."""
        payload = {"memory_id": "m1", "schema_version": 99}
        r = MemoryRecord.from_dict(payload)
        assert r.schema_version == 99

    def test_missing_schema_version_uses_default(self):
        payload = {"memory_id": "m1"}
        r = MemoryRecord.from_dict(payload)
        assert r.schema_version == MEMORY_SCHEMA_VERSION

    def test_round_trip_preserves_v1(self):
        r = MemoryRecord(memory_id="m1")
        d = r.to_dict()
        r2 = MemoryRecord.from_dict(d)
        assert r2.schema_version == r.schema_version


class TestIntelContextTupleToListSerialization:

    def test_top_level_tuples_become_lists(self):
        ic = IntelligenceContext(related_incident_ids=("a", "b"))
        d = ic.to_dict()
        # RC-I contract: every tuple becomes a list.
        assert type(d["related_incident_ids"]) is list
        assert d["related_incident_ids"] == ["a", "b"]

    def test_nested_dataclass_tuples_become_lists(self):
        from sentinel_core.models.intel_context import (
            AffectedService,
            DependencyEdge,
        )
        ic = IntelligenceContext(
            upstream_dependencies=(
                DependencyEdge(source_service="a", target_service="b",
                                 dep_type="http", strength=0.5),
            ),
            blast_radius_affected=(
                AffectedService(service_id="s", probability=0.9,
                                  propagation_ms=100),
            ),
        )
        d = ic.to_dict()
        assert type(d["upstream_dependencies"]) is list
        assert type(d["blast_radius_affected"]) is list
        # Each element is a dict (asdict decomposed the dataclass).
        assert type(d["upstream_dependencies"][0]) is dict

    def test_json_dumps_output_unchanged(self):
        """JSON serialization remains byte-identical — json.dumps treats
        tuples and lists identically. This test just confirms the
        contract fix does not disturb existing JSON consumers."""
        ic = IntelligenceContext(related_incident_ids=("a", "b"))
        j = json.dumps(ic.to_dict(), sort_keys=True)
        assert '"related_incident_ids": ["a", "b"]' in j

    def test_dict_round_trip_now_matches(self):
        """Post-fix, ic.to_dict() equals json.loads(json.dumps(...))."""
        ic = IntelligenceContext(related_incident_ids=("a", "b"))
        d = ic.to_dict()
        d2 = json.loads(json.dumps(d))
        assert d == d2


class TestDecisionContextTopServiceContract:
    """RC-I: verify the top_service docstring contract (highest-
    probability with lex tie-break) — Sprint 3 fixed the behavior;
    Sprint 4 pins it as an explicit contract test."""

    def test_top_service_is_highest_probability(self):
        from sentinel_core.models.intel_context import (
            AffectedService,
            IntelligenceContext,
        )
        from sentinel_core.models.decision_context import DecisionContext

        ic = IntelligenceContext(
            blast_radius_severity="high",
            blast_radius_total_affected=3,
            blast_radius_affected=(
                AffectedService(service_id="A", probability=0.1, propagation_ms=100),
                AffectedService(service_id="B", probability=0.5, propagation_ms=100),
                AffectedService(service_id="C", probability=0.9, propagation_ms=100),
            ),
        )
        dc = DecisionContext.from_intelligence_context(ic)
        assert dc.likely_blast_radius.top_service == "C"


# ---------------------------------------------------------------------------
# RC-K — Planner keyword token boundary matching
# ---------------------------------------------------------------------------

from supervisor.deterministic_planner.planner_rules import derive_goals


class _PC:
    """Minimal PlanContext duck for planner_rules.derive_goals."""
    def __init__(self, incident_type: str = "", service: str = ""):
        self.incident_type = incident_type
        self.service = service
        self.decision_context = None
        self.knowledge_graph = None
        self.completed_goals = ()
        self.outstanding_goals = ()


def _goal_types(pc: _PC) -> set[str]:
    return {g.goal_type for g in derive_goals(pc)}


class TestPlannerTokenBoundaryMatching:

    def test_reproduces_audit_defect_authoritative_dns_no_longer_triggers_auth(self):
        """Audit V-23 / RC-K core case.

        PRE-FIX: 'authoritative_dns_failure' triggered auth because
        substring 'auth' appeared in 'authoritative'. POST-FIX: auth is
        NOT triggered (whole-token match); DNS IS still triggered."""
        gt = _goal_types(_PC(incident_type="authoritative_dns_failure",
                              service="dns"))
        assert "determine_authentication_failure" not in gt
        assert "determine_network_failure" in gt

    def test_authentication_failure_triggers_auth(self):
        gt = _goal_types(_PC(incident_type="authentication_failure"))
        assert "determine_authentication_failure" in gt

    def test_token_validation_triggers_auth(self):
        gt = _goal_types(_PC(incident_type="token_validation_failure"))
        assert "determine_authentication_failure" in gt

    def test_dns_failure_triggers_network(self):
        gt = _goal_types(_PC(incident_type="dns_partial_outage"))
        assert "determine_network_failure" in gt

    def test_bare_auth_token_matches(self):
        """`auth_failure` should still trigger auth (bare 'auth' token)."""
        gt = _goal_types(_PC(incident_type="auth_failure"))
        assert "determine_authentication_failure" in gt

    def test_authorization_token_matches(self):
        gt = _goal_types(_PC(incident_type="authorization_denied"))
        assert "determine_authentication_failure" in gt

    def test_authn_and_authz_tokens_match(self):
        gt_n = _goal_types(_PC(incident_type="authn_check_failed"))
        gt_z = _goal_types(_PC(incident_type="authz_lookup_slow"))
        assert "determine_authentication_failure" in gt_n
        assert "determine_authentication_failure" in gt_z

    def test_storage_incidents_still_match(self):
        gt = _goal_types(_PC(incident_type="database_slow_query"))
        assert "determine_storage_bottleneck" in gt

    def test_network_timeout_still_matches(self):
        gt = _goal_types(_PC(incident_type="network_timeout"))
        assert "determine_network_failure" in gt

    def test_pod_crashloop_still_matches_k8s(self):
        gt = _goal_types(_PC(incident_type="pod_crashloop_backoff"))
        assert "validate_kubernetes_health" in gt

    def test_deployment_regression_still_triggers_deploy(self):
        gt = _goal_types(_PC(incident_type="deployment_regression"))
        assert "validate_deployment_hypothesis" in gt


# ---------------------------------------------------------------------------
# RC-H — SentinelBench string-iteration hole
# ---------------------------------------------------------------------------

from tests.synthetic.scoring import (
    score_decision_trace_quality,
    score_evidence_completeness,
)


class TestBenchScoringStringSequenceGuard:

    def test_reproduces_audit_defect_string_no_longer_iterated_as_chars(self):
        """PRE-FIX: score_evidence_completeness(('a',), 'abc') iterated
        the string as characters and returned 1.0. POST-FIX: 'abc' is
        treated as a single scalar → 'a' not in {'abc'} → 0.0."""
        s = score_evidence_completeness(("a",), "abc")
        assert s == 0.0

    def test_decision_trace_string_scalar(self):
        s = score_decision_trace_quality(("signal_a",), "signal_a")
        # 'signal_a' is scalar → set is {'signal_a'} → hit → 1.0
        assert s == 1.0

    def test_decision_trace_string_mismatch(self):
        s = score_decision_trace_quality(("signal_a",), "different")
        assert s == 0.0

    def test_list_input_still_works(self):
        s = score_evidence_completeness(("a", "b"), ["a", "b", "c"])
        assert s == 1.0

    def test_tuple_input_still_works(self):
        s = score_evidence_completeness(("a",), ("a", "b"))
        assert s == 1.0

    def test_missing_required_yields_partial(self):
        s = score_evidence_completeness(("a", "b"), ("a",))
        assert s == 0.5
