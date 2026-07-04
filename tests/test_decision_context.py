"""Deterministic-transform tests for
sentinel_core.models.decision_context.DecisionContext.

Pure-library tests: no runtime, no store, no fixture side effects. Each
case constructs a synthetic IntelligenceContext and verifies the
DecisionContext factory's mapping table.
"""
from __future__ import annotations

import json
import pytest

from sentinel_core.models.decision_context import (
    BlastRadiusProjection,
    ConfidenceAdjustment,
    DecisionContext,
    _EXPECTED_MODULES,
)
from sentinel_core.models.intel_context import (
    AffectedService,
    DependencyEdge,
    EpisodeMatch,
    IntelligenceContext,
    InvestigationMatch,
    PatternMatch,
    ResolutionMemoryMatch,
)


# ---------------------------------------------------------------------------
# Empty / defaults
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_from_empty_intel_context_produces_valid_default(self):
        d = DecisionContext.from_intelligence_context(IntelligenceContext())
        assert d.is_empty()
        assert d.confidence == 50
        assert d.investigation_priority == "low"
        # All six sources are gaps when nothing was seen
        assert set(d.evidence_gaps) == set(_EXPECTED_MODULES)
        # The terminal "collect_evidence" step is always present
        assert d.recommended_investigation_order == ("collect_evidence",)

    def test_none_input_treated_as_empty(self):
        d = DecisionContext.from_intelligence_context(None)
        assert d.is_empty()

    def test_dict_input_supported(self):
        # Duck-typing: dict with matching keys works
        d = DecisionContext.from_intelligence_context({
            "service": "s", "incident_type": "t",
        })
        assert d.service == "s"
        assert d.incident_type == "t"


# ---------------------------------------------------------------------------
# Identity + failure-type mapping
# ---------------------------------------------------------------------------

class TestIdentity:
    def test_service_and_incident_type_propagate(self):
        ic = IntelligenceContext(service="checkout", incident_type="saturation")
        d = DecisionContext.from_intelligence_context(ic)
        assert d.service == "checkout"
        assert d.incident_type == "saturation"

    def test_likely_failure_type_prefers_top_recurring_pattern(self):
        ic = IntelligenceContext(
            service="checkout", incident_type="saturation",
            pattern_matches=(
                PatternMatch(pattern_id="p", incident_type="db_pool_exhaustion",
                              occurrence_count=5, success_count=4, success_rate=0.8),
            ),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.likely_failure_type == "db_pool_exhaustion"

    def test_likely_failure_type_falls_back_to_classification(self):
        ic = IntelligenceContext(service="s", incident_type="saturation")
        d = DecisionContext.from_intelligence_context(ic)
        assert d.likely_failure_type == "saturation"


# ---------------------------------------------------------------------------
# Recurring + historical_success_rate
# ---------------------------------------------------------------------------

class TestRecurringAndSuccessRate:
    def test_single_occurrence_pattern_not_recurring(self):
        ic = IntelligenceContext(
            pattern_matches=(
                PatternMatch(pattern_id="p", incident_type="t",
                              occurrence_count=1, success_rate=0.9),
            ),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.recurring_incident is False
        # But historical_success_rate still surfaces the max rate
        assert d.historical_success_rate == 0.9

    def test_two_plus_occurrence_pattern_is_recurring(self):
        ic = IntelligenceContext(
            pattern_matches=(
                PatternMatch(pattern_id="p", incident_type="t",
                              occurrence_count=3, success_rate=0.6),
            ),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.recurring_incident is True
        assert d.historical_success_rate == 0.6


# ---------------------------------------------------------------------------
# Blast-radius projection
# ---------------------------------------------------------------------------

class TestBlastRadius:
    def test_projection_captures_severity_and_top_service(self):
        ic = IntelligenceContext(
            blast_radius_severity="high",
            blast_radius_total_affected=3,
            blast_radius_affected=(
                AffectedService(service_id="cart-api", probability=0.9,
                                  propagation_ms=100),
                AffectedService(service_id="ui-web", probability=0.7,
                                  propagation_ms=200),
            ),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert isinstance(d.likely_blast_radius, BlastRadiusProjection)
        assert d.likely_blast_radius.severity == "high"
        assert d.likely_blast_radius.total_affected == 3
        assert d.likely_blast_radius.top_service == "cart-api"


# ---------------------------------------------------------------------------
# Recommended next service
# ---------------------------------------------------------------------------

class TestRecommendedNextService:
    def test_downstream_by_max_strength(self):
        ic = IntelligenceContext(
            downstream_dependents=(
                DependencyEdge(source_service="ui-web",
                                target_service="checkout",
                                dep_type="runtime", strength=0.4),
                DependencyEdge(source_service="cart-api",
                                target_service="checkout",
                                dep_type="runtime", strength=0.9),
            ),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.recommended_next_service == "cart-api"

    def test_falls_back_to_affected_services(self):
        ic = IntelligenceContext(
            affected_services=("cart-api", "ui-web"),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.recommended_next_service == "cart-api"


# ---------------------------------------------------------------------------
# Investigation order
# ---------------------------------------------------------------------------

class TestInvestigationOrder:
    def test_order_reflects_signals_present(self):
        ic = IntelligenceContext(
            upstream_dependencies=(DependencyEdge(
                source_service="checkout", target_service="db",
                dep_type="runtime", strength=0.8),),
            pattern_matches=(PatternMatch(
                pattern_id="p", incident_type="t", occurrence_count=3,
                success_count=2, success_rate=0.66),),
            resolution_memory_matches=(ResolutionMemoryMatch(
                memory_id="m", root_cause_head="", confidence=80,
                recorded_at=""),),
            blast_radius_severity="high",
        )
        d = DecisionContext.from_intelligence_context(ic)
        # Deterministic prefix ordering
        order = list(d.recommended_investigation_order)
        assert "check_upstream_dependencies" in order
        assert "compare_recurring_pattern" in order
        assert "review_prior_resolutions" in order
        assert "assess_blast_radius" in order
        assert order[-1] == "collect_evidence"

    def test_empty_intel_yields_only_terminal_step(self):
        d = DecisionContext.from_intelligence_context(IntelligenceContext())
        assert d.recommended_investigation_order == ("collect_evidence",)


# ---------------------------------------------------------------------------
# Recommended queries
# ---------------------------------------------------------------------------

class TestRecommendedQueries:
    def test_queries_track_signals(self):
        ic = IntelligenceContext(
            service="checkout",
            upstream_dependencies=(DependencyEdge(source_service="checkout",
                                                    target_service="db",
                                                    dep_type="runtime",
                                                    strength=0.7),),
            downstream_dependents=(DependencyEdge(source_service="ui",
                                                    target_service="checkout",
                                                    dep_type="runtime",
                                                    strength=0.5),),
            resolution_memory_matches=(ResolutionMemoryMatch(memory_id="m",
                                                                root_cause_head="",
                                                                confidence=70,
                                                                recorded_at=""),),
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=3,
                                            success_count=2,
                                            success_rate=0.66),),
            blast_radius_severity="critical",
        )
        d = DecisionContext.from_intelligence_context(ic)
        for q in ("logs_for_service", "upstream_service_health",
                   "downstream_service_health", "prior_resolution_actions",
                   "pattern_playbook", "blast_radius_dashboard"):
            assert q in d.recommended_queries


# ---------------------------------------------------------------------------
# Confidence adjustments
# ---------------------------------------------------------------------------

class TestConfidenceAdjustments:
    def test_base_50_no_signals(self):
        d = DecisionContext.from_intelligence_context(IntelligenceContext())
        assert d.confidence == 50
        assert d.confidence_adjustments == ()

    def test_adjustments_sum_to_confidence(self):
        ic = IntelligenceContext(
            resolution_memory_matches=(ResolutionMemoryMatch(memory_id="m",
                                                                root_cause_head="",
                                                                confidence=80,
                                                                recorded_at=""),),
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=3,
                                            success_count=2,
                                            success_rate=0.66),),
            related_incident_ids=("INC_1",),
            episode_matches=(EpisodeMatch(episode_id="e", incident_id="",
                                            service="", incident_type="",
                                            root_cause_head="",
                                            resolution_action_head="",
                                            outcome="resolved",
                                            confidence=0.7, recorded_at=""),),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.confidence == 50 + sum(a.delta for a in d.confidence_adjustments)
        reasons = {a.reason for a in d.confidence_adjustments}
        assert "have_prior_resolution_memory" in reasons
        assert "recurring_pattern_seen" in reasons
        assert "have_related_incidents" in reasons
        assert "have_prior_episodes" in reasons
        # historical_success_rate >= 0.5 → high_historical_success_rate
        assert "high_historical_success_rate" in reasons

    def test_confidence_clamped_to_100(self):
        # Even if we stack every signal, confidence must not exceed 100.
        ic = IntelligenceContext(
            resolution_memory_matches=(ResolutionMemoryMatch(memory_id="m",
                                                                root_cause_head="",
                                                                confidence=90,
                                                                recorded_at=""),),
            investigation_matches=(InvestigationMatch(investigation_id="i",
                                                        created_at=""),),
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=3, success_count=3,
                                            success_rate=1.0),),
            related_incident_ids=tuple(f"INC_{i}" for i in range(5)),
            episode_matches=(EpisodeMatch(episode_id="e", incident_id="",
                                            service="", incident_type="",
                                            root_cause_head="",
                                            resolution_action_head="",
                                            outcome="resolved",
                                            confidence=1.0, recorded_at=""),),
            blast_radius_severity="critical",
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.confidence <= 100
        assert d.confidence >= 50


# ---------------------------------------------------------------------------
# Evidence gaps
# ---------------------------------------------------------------------------

class TestEvidenceGaps:
    def test_empty_module_names_seen_marks_all_as_gaps(self):
        d = DecisionContext.from_intelligence_context(IntelligenceContext())
        assert set(d.evidence_gaps) == set(_EXPECTED_MODULES)

    def test_seen_module_removed_from_gaps(self):
        ic = IntelligenceContext(
            module_names_seen=("historical_lookup", "pattern_recognition"),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert "historical_lookup" not in d.evidence_gaps
        assert "pattern_recognition" not in d.evidence_gaps
        assert "causal_graph_lookup" in d.evidence_gaps


# ---------------------------------------------------------------------------
# Investigation priority
# ---------------------------------------------------------------------------

class TestPriority:
    def test_critical_severity(self):
        ic = IntelligenceContext(blast_radius_severity="critical")
        d = DecisionContext.from_intelligence_context(ic)
        assert d.investigation_priority == "critical"

    def test_high_affected_count_promotes_to_critical(self):
        ic = IntelligenceContext(blast_radius_severity="low",
                                   blast_radius_total_affected=5)
        d = DecisionContext.from_intelligence_context(ic)
        assert d.investigation_priority == "critical"

    def test_recurring_low_success_rate_is_high(self):
        ic = IntelligenceContext(
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=3,
                                            success_count=0,
                                            success_rate=0.1),),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.investigation_priority == "high"

    def test_medium_when_any_signal_present(self):
        ic = IntelligenceContext(
            resolution_memory_matches=(ResolutionMemoryMatch(memory_id="m",
                                                                root_cause_head="",
                                                                confidence=50,
                                                                recorded_at=""),),
        )
        d = DecisionContext.from_intelligence_context(ic)
        assert d.investigation_priority == "medium"


# ---------------------------------------------------------------------------
# Serialization + determinism + immutability
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_is_json_safe(self):
        ic = IntelligenceContext(
            service="checkout", incident_type="saturation",
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=2, success_count=1,
                                            success_rate=0.5),),
        )
        d = DecisionContext.from_intelligence_context(ic)
        s = json.dumps(d.to_dict())
        d2 = json.loads(s)
        assert d2["confidence"] == d.confidence
        assert d2["service"] == "checkout"

    def test_transform_is_byte_deterministic(self):
        # Same input → identical dict output on repeated calls
        ic = IntelligenceContext(
            service="checkout", incident_type="saturation",
            pattern_matches=(PatternMatch(pattern_id="p", incident_type="t",
                                            occurrence_count=2, success_count=1,
                                            success_rate=0.5),),
            resolution_memory_matches=(ResolutionMemoryMatch(memory_id="m",
                                                                root_cause_head="",
                                                                confidence=70,
                                                                recorded_at=""),),
        )
        d1 = DecisionContext.from_intelligence_context(ic).to_dict()
        d2 = DecisionContext.from_intelligence_context(ic).to_dict()
        assert d1 == d2
        # And json-encoded byte-identity
        assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)

    def test_container_frozen(self):
        d = DecisionContext()
        with pytest.raises(Exception):
            d.confidence = 99


# ---------------------------------------------------------------------------
# Malformed input tolerance
# ---------------------------------------------------------------------------

class TestMalformedInput:
    def test_object_with_only_some_fields(self):
        """A stripped-down mock with subset of attrs should still work."""
        class _Mock:
            service = "s"
            incident_type = "t"
        d = DecisionContext.from_intelligence_context(_Mock())
        assert d.service == "s"
        assert d.incident_type == "t"
        # All other fields default
        assert d.confidence == 50

    def test_bad_types_do_not_crash(self):
        """Malformed intelligence handling — the factory MUST NOT crash on
        wrong types. Bad values fall through to defaults."""
        class _Mock:
            service = None                          # None → ""
            incident_type = 123                     # coerced via str()
            pattern_matches = "not-a-tuple"         # str → () via _as_tuple
            related_incident_ids = None             # None → ()
            blast_radius_severity = None            # None → "low"
            blast_radius_total_affected = "not-an-int"  # bad → 0 via _as_int
            module_names_seen = ()
        d = DecisionContext.from_intelligence_context(_Mock())
        # Fell through cleanly
        assert d.service == ""
        assert d.incident_type == "123"
        assert d.likely_blast_radius.severity == "low"
        assert d.likely_blast_radius.total_affected == 0
        assert d.confidence == 50
        assert d.investigation_priority == "low"

    def test_iterables_that_arent_lists_still_work(self):
        """A generator-style iterable should also be accepted."""
        class _Mock:
            related_incident_ids = iter(["INC1", "INC2"])
            module_names_seen = ()
        d = DecisionContext.from_intelligence_context(_Mock())
        # Signal was recorded (medium priority) even though the input was a generator
        assert d.investigation_priority == "medium"
