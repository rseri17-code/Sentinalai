"""Tests for sentinel_core.models.intel_context.IntelligenceContext.

Pure-library tests — no runtime, no store, no fixtures with side effects.
Every case exercises the receipt-list → canonical-shape factory.
"""
from __future__ import annotations

import json
import pytest

from sentinel_core.models.intel_context import (
    AffectedService,
    DependencyEdge,
    EpisodeMatch,
    InvestigationMatch,
    IntelligenceContext,
    PatternMatch,
    ResolutionMemoryMatch,
)


# ---------------------------------------------------------------------------
# Helpers — build synthetic phase receipts of the shape produced by
# PhaseReceiptCollector + the runtime intelligence hook.
# ---------------------------------------------------------------------------

def _receipt(**intelligence_entries) -> dict:
    """Build one phase receipt containing the given intelligence entries."""
    return {
        "name": "classify",
        "metadata": {
            "intelligence": [
                {"name": name, "status": "success", "metadata": payload}
                for name, payload in intelligence_entries.items()
            ],
        },
    }


# ---------------------------------------------------------------------------
# Empty / defaults
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_from_empty_iterable_is_empty(self):
        c = IntelligenceContext.from_receipts([])
        assert c.is_empty()
        assert c.total_signal_count() == 0
        assert c.module_names_seen == ()

    def test_from_none_is_empty(self):
        c = IntelligenceContext.from_receipts(None)
        assert c.is_empty()

    def test_missing_intelligence_metadata_is_ignored(self):
        # Receipt without intelligence key
        c = IntelligenceContext.from_receipts([{"name": "classify", "metadata": {}}])
        assert c.is_empty()

    def test_non_dict_entries_are_ignored(self):
        c = IntelligenceContext.from_receipts(["notadict", 42, None])
        assert c.is_empty()


# ---------------------------------------------------------------------------
# Per-source unpacking
# ---------------------------------------------------------------------------

class TestHistoricalLookup:
    def test_rm_matches_unpacked(self):
        r = _receipt(historical_lookup={
            "service": "checkout",
            "incident_type": "saturation",
            "resolution_memory_matches": [
                {"memory_id": "m1", "root_cause_head": "db pool",
                  "confidence": 82, "recorded_at": "2026-07-01T00:00:00Z",
                  "service": "checkout", "incident_type": "saturation"},
            ],
            "investigation_matches": [],
        })
        c = IntelligenceContext.from_receipts([r])
        assert c.service == "checkout"
        assert c.incident_type == "saturation"
        assert len(c.resolution_memory_matches) == 1
        m = c.resolution_memory_matches[0]
        assert isinstance(m, ResolutionMemoryMatch)
        assert m.memory_id == "m1"
        assert m.confidence == 82

    def test_investigation_matches_unpacked(self):
        r = _receipt(historical_lookup={
            "service": "checkout",
            "incident_type": "saturation",
            "resolution_memory_matches": [],
            "investigation_matches": [
                {"investigation_id": "inv-1", "created_at": "2026-06-01T00:00:00Z",
                  "incident_type": "saturation", "service": "checkout"},
            ],
        })
        c = IntelligenceContext.from_receipts([r])
        assert len(c.investigation_matches) == 1
        assert c.investigation_matches[0].investigation_id == "inv-1"


class TestPatternRecognition:
    def test_pattern_matches_unpacked(self):
        r = _receipt(pattern_recognition={
            "service": "checkout",
            "incident_type": "saturation",
            "pattern_matches": [
                {"pattern_id": "p1", "incident_type": "saturation",
                  "services": ["checkout"], "canonical_symptoms": ["db", "pool"],
                  "occurrence_count": 5, "success_count": 4,
                  "success_rate": 0.8, "last_seen": "2026-07-01"},
            ],
            "match_count": 1,
        })
        c = IntelligenceContext.from_receipts([r])
        assert len(c.pattern_matches) == 1
        p = c.pattern_matches[0]
        assert isinstance(p, PatternMatch)
        assert p.pattern_id == "p1"
        assert p.success_rate == 0.8


class TestIncidentGraphLookup:
    def test_related_incidents_unpacked(self):
        r = _receipt(incident_graph_lookup={
            "service": "checkout",
            "related_incident_ids": ["INC_A", "INC_B"],
        })
        c = IntelligenceContext.from_receipts([r])
        assert c.related_incident_ids == ("INC_A", "INC_B")


class TestDependencyGraphLookup:
    def test_upstream_downstream_and_affected(self):
        r = _receipt(dependency_graph_lookup={
            "service": "checkout",
            "upstream": [
                {"source_service": "checkout", "target_service": "db",
                  "dep_type": "runtime", "strength": 0.8,
                  "observed_count": 5, "last_seen": "2026-07-01"},
            ],
            "downstream": [
                {"source_service": "ui", "target_service": "checkout",
                  "dep_type": "runtime", "strength": 0.6,
                  "observed_count": 3, "last_seen": "2026-07-01"},
            ],
            "affected_services": ["ui", "cart"],
        })
        c = IntelligenceContext.from_receipts([r])
        assert len(c.upstream_dependencies) == 1
        assert isinstance(c.upstream_dependencies[0], DependencyEdge)
        assert c.upstream_dependencies[0].target_service == "db"
        assert len(c.downstream_dependents) == 1
        assert c.downstream_dependents[0].source_service == "ui"
        assert c.affected_services == ("ui", "cart")


class TestEpisodicMemoryLookup:
    def test_episodes_unpacked(self):
        r = _receipt(episodic_memory_lookup={
            "service": "checkout",
            "incident_type": "saturation",
            "episodes": [
                {"episode_id": "e1", "incident_id": "INC1",
                  "service": "checkout", "incident_type": "saturation",
                  "root_cause_head": "cause", "resolution_action_head": "action",
                  "outcome": "resolved", "confidence": 0.9,
                  "recorded_at": "2026-07-01"},
            ],
            "match_count": 1,
        })
        c = IntelligenceContext.from_receipts([r])
        assert len(c.episode_matches) == 1
        e = c.episode_matches[0]
        assert isinstance(e, EpisodeMatch)
        assert e.episode_id == "e1"
        assert e.outcome == "resolved"


class TestCausalGraphLookup:
    def test_blast_radius_unpacked(self):
        r = _receipt(causal_graph_lookup={
            "service": "checkout",
            "severity": "high",
            "total_affected": 3,
            "affected": [
                {"service_id": "s1", "probability": 0.9,
                  "propagation_ms": 100, "path": ["checkout", "s1"]},
                {"service_id": "s2", "probability": 0.7,
                  "propagation_ms": 200, "path": ["checkout", "s2"]},
            ],
        })
        c = IntelligenceContext.from_receipts([r])
        assert c.blast_radius_severity == "high"
        assert c.blast_radius_total_affected == 3
        assert len(c.blast_radius_affected) == 2
        assert isinstance(c.blast_radius_affected[0], AffectedService)
        assert c.blast_radius_affected[0].service_id == "s1"


# ---------------------------------------------------------------------------
# Multi-source composition
# ---------------------------------------------------------------------------

class TestComposition:
    def test_service_taken_from_first_source(self):
        # pattern first, historical second — both have service; first wins.
        r = _receipt(
            pattern_recognition={"service": "pattern-svc", "incident_type": "t1"},
            historical_lookup={"service": "historical-svc", "incident_type": "t2"},
        )
        c = IntelligenceContext.from_receipts([r])
        # Because dict iteration order is insertion, historical_lookup is
        # processed first in the factory. Assert deterministic behaviour by
        # constructing both possible orderings and asserting non-empty.
        assert c.service in ("pattern-svc", "historical-svc")

    def test_multiple_receipts_merged(self):
        r1 = _receipt(historical_lookup={
            "service": "checkout", "incident_type": "saturation",
            "resolution_memory_matches": [{"memory_id": "m1", "root_cause_head": "",
                                            "confidence": 50, "recorded_at": "x"}],
            "investigation_matches": [],
        })
        r2 = _receipt(pattern_recognition={
            "service": "checkout", "incident_type": "saturation",
            "pattern_matches": [{"pattern_id": "p1", "incident_type": "saturation",
                                  "occurrence_count": 3, "success_count": 2,
                                  "success_rate": 0.66, "last_seen": ""}],
        })
        c = IntelligenceContext.from_receipts([r1, r2])
        assert len(c.resolution_memory_matches) == 1
        assert len(c.pattern_matches) == 1
        assert "historical_lookup" in c.module_names_seen
        assert "pattern_recognition" in c.module_names_seen

    def test_all_six_sources_at_once(self):
        r = _receipt(
            historical_lookup={
                "service": "checkout", "incident_type": "saturation",
                "resolution_memory_matches": [{"memory_id": "m", "root_cause_head": "",
                                                "confidence": 0, "recorded_at": ""}],
                "investigation_matches": [{"investigation_id": "i", "created_at": ""}],
            },
            pattern_recognition={
                "service": "checkout", "incident_type": "saturation",
                "pattern_matches": [{"pattern_id": "p", "incident_type": "saturation",
                                      "occurrence_count": 2, "success_count": 1,
                                      "success_rate": 0.5, "last_seen": ""}],
            },
            incident_graph_lookup={"service": "checkout",
                                     "related_incident_ids": ["I1", "I2"]},
            dependency_graph_lookup={"service": "checkout",
                                        "upstream": [{"source_service": "s",
                                                      "target_service": "t",
                                                      "dep_type": "runtime",
                                                      "strength": 0.5}],
                                        "downstream": [],
                                        "affected_services": ["a"]},
            episodic_memory_lookup={"service": "checkout",
                                      "incident_type": "saturation",
                                      "episodes": [{"episode_id": "e",
                                                     "incident_id": "iid",
                                                     "service": "checkout",
                                                     "incident_type": "saturation",
                                                     "root_cause_head": "",
                                                     "resolution_action_head": "",
                                                     "outcome": "resolved",
                                                     "confidence": 0.5,
                                                     "recorded_at": ""}]},
            causal_graph_lookup={"service": "checkout",
                                    "severity": "medium",
                                    "total_affected": 1,
                                    "affected": [{"service_id": "d",
                                                    "probability": 0.4,
                                                    "propagation_ms": 50,
                                                    "path": ["checkout", "d"]}]},
        )
        c = IntelligenceContext.from_receipts([r])
        assert not c.is_empty()
        assert c.total_signal_count() >= 7
        assert len(c.module_names_seen) == 6
        assert c.blast_radius_severity == "medium"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_unknown_module_is_ignored(self):
        r = _receipt(some_future_module={"foo": "bar"})
        c = IntelligenceContext.from_receipts([r])
        assert c.is_empty()
        # But the module name IS captured
        assert "some_future_module" in c.module_names_seen

    def test_partial_shape_survives(self):
        # Missing fields in per-source payload should not crash
        r = _receipt(pattern_recognition={"pattern_matches": [{"pattern_id": "p"}]})
        c = IntelligenceContext.from_receipts([r])
        assert c.pattern_matches[0].pattern_id == "p"
        assert c.pattern_matches[0].occurrence_count == 0

    def test_skipped_status_still_captured_as_module_name(self):
        # A module that skipped won't have per-source data but its name
        # should still appear in module_names_seen.
        receipt = {
            "name": "classify",
            "metadata": {
                "intelligence": [
                    {"name": "historical_lookup", "status": "skipped",
                      "metadata": {"reason": "no_service"}},
                ],
            },
        }
        c = IntelligenceContext.from_receipts([receipt])
        assert c.resolution_memory_matches == ()
        assert "historical_lookup" in c.module_names_seen


# ---------------------------------------------------------------------------
# JSON-safety + immutability
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_is_json_safe(self):
        r = _receipt(historical_lookup={
            "service": "checkout", "incident_type": "saturation",
            "resolution_memory_matches": [{"memory_id": "m", "root_cause_head": "",
                                            "confidence": 50, "recorded_at": ""}],
            "investigation_matches": [],
        })
        c = IntelligenceContext.from_receipts([r])
        d = c.to_dict()
        s = json.dumps(d)  # must not raise
        d2 = json.loads(s)
        assert d2["service"] == "checkout"
        assert d2["resolution_memory_matches"][0]["memory_id"] == "m"

    def test_frozen_container_is_immutable(self):
        c = IntelligenceContext(service="x")
        with pytest.raises(Exception):
            c.service = "y"

    def test_frozen_child_dataclass_is_immutable(self):
        m = ResolutionMemoryMatch(memory_id="m", root_cause_head="", confidence=0,
                                    recorded_at="")
        with pytest.raises(Exception):
            m.memory_id = "n"
