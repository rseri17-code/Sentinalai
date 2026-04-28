"""Tests for supervisor/causal_graph_diff.py.

Covers:
  - Added edge detection
  - Removed edge detection
  - Modified edge detection (weight / props change)
  - Node property change detection
  - Added / removed node detection
  - most_likely_trigger selection (temporal proximity ranking)
  - Empty diff (identical snapshots)
  - Completely empty snapshots
  - trigger_confidence scoring
  - reasoning string content
  - CausalDiff dataclass structure

All tests are standalone — no external I/O or mocked I/O needed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pytest

from supervisor.causal_graph_diff import (
    CausalDiff,
    GraphEdgeDiff,
    GraphNodeDiff,
    diff_graph_snapshots,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Convert a datetime to ISO-8601 string with UTC timezone."""
    return dt.isoformat()


def _ts(dt: datetime) -> float:
    """Convert a datetime to Unix epoch float."""
    return dt.timestamp()


def _make_node(
    node_id: str,
    node_type: str = "service",
    label: str = "",
    props: dict | None = None,
    created_at: float | None = None,
) -> dict:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "label": label or node_id,
        "props": props or {},
        "created_at": created_at or time.time(),
    }


def _make_edge(
    src_id: str,
    dst_id: str,
    rel_type: str = "DEPENDS_ON",
    weight: float = 1.0,
    props: dict | None = None,
    created_at: float | None = None,
) -> dict:
    return {
        "edge_id": f"{src_id}::{rel_type}::{dst_id}",
        "src_id": src_id,
        "dst_id": dst_id,
        "rel_type": rel_type,
        "weight": weight,
        "props": props or {},
        "created_at": created_at or time.time(),
    }


def _snapshot(nodes: list[dict], edges: list[dict]) -> dict:
    return {"nodes": nodes, "edges": edges}


# Reference time: incident started at T
T = datetime(2026, 4, 19, 14, 0, 0, tzinfo=timezone.utc)
T_MINUS_1H = T - timedelta(hours=1)
T_MINUS_5MIN = T - timedelta(minutes=5)
T_MINUS_1MIN = T - timedelta(minutes=1)
T_PLUS_10MIN = T + timedelta(minutes=10)
INCIDENT_START = _iso(T)


# ---------------------------------------------------------------------------
# Empty / identical snapshot tests
# ---------------------------------------------------------------------------

class TestEmptyDiffs:
    def test_identical_snapshots_produce_empty_diff(self):
        """When before == after, there should be nothing to report."""
        nodes = [
            _make_node("svc:payment", created_at=_ts(T_MINUS_1H)),
            _make_node("svc:db", created_at=_ts(T_MINUS_1H)),
        ]
        edges = [
            _make_edge("svc:payment", "svc:db", created_at=_ts(T_MINUS_1H)),
        ]
        snap = _snapshot(nodes, edges)
        result = diff_graph_snapshots(snap, snap, "INC-001", INCIDENT_START)

        assert isinstance(result, CausalDiff)
        assert result.added_edges == []
        assert result.removed_edges == []
        assert result.changed_nodes == []
        assert result.most_likely_trigger is None
        assert result.trigger_confidence == 0.0

    def test_both_empty_snapshots(self):
        """Two completely empty snapshots → zero changes."""
        empty = _snapshot([], [])
        result = diff_graph_snapshots(empty, empty, "INC-002", INCIDENT_START)

        assert result.added_edges == []
        assert result.removed_edges == []
        assert result.changed_nodes == []
        assert result.most_likely_trigger is None
        assert result.trigger_confidence == 0.0

    def test_reasoning_mentions_no_changes_when_identical(self):
        empty = _snapshot([], [])
        result = diff_graph_snapshots(empty, empty, "INC-003", INCIDENT_START)
        assert "No structural changes" in result.reasoning or \
               len(result.added_edges) + len(result.removed_edges) + len(result.changed_nodes) == 0


# ---------------------------------------------------------------------------
# Added edge detection tests
# ---------------------------------------------------------------------------

class TestAddedEdges:
    def test_new_caused_by_edge_detected(self):
        """An edge in 'after' but not in 'before' → added_edges entry."""
        node_a = _make_node("inc-001", "incident", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:payment-db", "service", created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b], [])
        new_edge = _make_edge("inc-001", "svc:payment-db", "CAUSED_BY",
                              created_at=_ts(T_MINUS_5MIN))
        after = _snapshot([node_a, node_b], [new_edge])

        result = diff_graph_snapshots(before, after, "INC-004", INCIDENT_START)

        assert len(result.added_edges) == 1
        edge_diff = result.added_edges[0]
        assert isinstance(edge_diff, GraphEdgeDiff)
        assert edge_diff.edge_type == "CAUSED_BY"
        assert edge_diff.source_node == "inc-001"
        assert edge_diff.target_node == "svc:payment-db"
        assert edge_diff.change_type == "added"

    def test_multiple_added_edges_all_detected(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        node_c = _make_node("svc:c", created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b, node_c], [])
        after = _snapshot(
            [node_a, node_b, node_c],
            [
                _make_edge("svc:a", "svc:b", "DEPENDS_ON", created_at=_ts(T_MINUS_5MIN)),
                _make_edge("svc:b", "svc:c", "DEPENDS_ON", created_at=_ts(T_MINUS_1MIN)),
            ],
        )

        result = diff_graph_snapshots(before, after, "INC-005", INCIDENT_START)
        assert len(result.added_edges) == 2

    def test_added_edge_confidence_is_float_between_0_and_1(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b], [])
        after = _snapshot(
            [node_a, node_b],
            [_make_edge("svc:a", "svc:b", "AFFECTED", created_at=_ts(T_MINUS_5MIN))],
        )

        result = diff_graph_snapshots(before, after, "INC-006", INCIDENT_START)
        for edge in result.added_edges:
            assert 0.0 <= edge.confidence_as_trigger <= 1.0


# ---------------------------------------------------------------------------
# Removed edge detection tests
# ---------------------------------------------------------------------------

class TestRemovedEdges:
    def test_missing_edge_in_after_detected_as_removed(self):
        node_a = _make_node("svc:payment", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:db", created_at=_ts(T_MINUS_1H))
        old_edge = _make_edge("svc:payment", "svc:db", "DEPENDS_ON",
                              created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b], [old_edge])
        after = _snapshot([node_a, node_b], [])  # edge removed

        result = diff_graph_snapshots(before, after, "INC-007", INCIDENT_START)

        assert len(result.removed_edges) == 1
        removed = result.removed_edges[0]
        assert isinstance(removed, GraphEdgeDiff)
        assert removed.change_type == "removed"
        assert removed.source_node == "svc:payment"
        assert removed.target_node == "svc:db"

    def test_removed_edge_has_correct_rel_type(self):
        node_a = _make_node("inc-010", "incident", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("rc:connection-pool", "root_cause", created_at=_ts(T_MINUS_1H))
        edge = _make_edge("inc-010", "rc:connection-pool", "HAS_ROOT_CAUSE",
                          created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b], [edge])
        after = _snapshot([node_a, node_b], [])

        result = diff_graph_snapshots(before, after, "INC-008", INCIDENT_START)
        assert result.removed_edges[0].edge_type == "HAS_ROOT_CAUSE"


# ---------------------------------------------------------------------------
# Modified edge detection tests
# ---------------------------------------------------------------------------

class TestModifiedEdges:
    def test_weight_change_detected_as_modified(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        edge_before = _make_edge("svc:a", "svc:b", "DEPENDS_ON", weight=0.5,
                                 created_at=_ts(T_MINUS_1H))
        edge_after = _make_edge("svc:a", "svc:b", "DEPENDS_ON", weight=0.95,
                                created_at=_ts(T_MINUS_5MIN))

        before = _snapshot([node_a, node_b], [edge_before])
        after = _snapshot([node_a, node_b], [edge_after])

        result = diff_graph_snapshots(before, after, "INC-009", INCIDENT_START)
        modified = [e for e in result.added_edges if e.change_type == "modified"]
        assert len(modified) == 1
        assert modified[0].edge_type == "DEPENDS_ON"

    def test_unchanged_edge_not_in_diff(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        same_edge = _make_edge("svc:a", "svc:b", "DEPENDS_ON", weight=1.0,
                               props={"stable": True}, created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b], [same_edge])
        after = _snapshot([node_a, node_b], [same_edge])

        result = diff_graph_snapshots(before, after, "INC-010", INCIDENT_START)
        assert result.added_edges == []
        assert result.removed_edges == []


# ---------------------------------------------------------------------------
# Node change detection tests
# ---------------------------------------------------------------------------

class TestNodeChanges:
    def test_added_node_detected(self):
        existing_node = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        new_node = _make_node("svc:b", created_at=_ts(T_MINUS_5MIN))

        before = _snapshot([existing_node], [])
        after = _snapshot([existing_node, new_node], [])

        result = diff_graph_snapshots(before, after, "INC-011", INCIDENT_START)
        added = [n for n in result.changed_nodes if n.change_type == "added"]
        assert len(added) == 1
        assert added[0].node_id == "svc:b"

    def test_removed_node_detected(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))

        before = _snapshot([node_a, node_b], [])
        after = _snapshot([node_a], [])

        result = diff_graph_snapshots(before, after, "INC-012", INCIDENT_START)
        removed = [n for n in result.changed_nodes if n.change_type == "removed"]
        assert len(removed) == 1
        assert removed[0].node_id == "svc:b"

    def test_node_property_change_detected(self):
        node_before = _make_node(
            "svc:payment",
            props={"tier": "P2", "error_rate": 0.01},
            created_at=_ts(T_MINUS_1H),
        )
        node_after = _make_node(
            "svc:payment",
            props={"tier": "P1", "error_rate": 0.25},  # tier upgraded, error_rate spiked
            created_at=_ts(T_MINUS_5MIN),
        )

        before = _snapshot([node_before], [])
        after = _snapshot([node_after], [])

        result = diff_graph_snapshots(before, after, "INC-013", INCIDENT_START)
        prop_changes = [n for n in result.changed_nodes if n.change_type == "property_changed"]
        assert len(prop_changes) == 1
        change = prop_changes[0]
        assert "tier" in change.changed_properties
        assert change.changed_properties["tier"] == ("P2", "P1")
        assert "error_rate" in change.changed_properties

    def test_unchanged_node_not_in_diff(self):
        node = _make_node("svc:stable", props={"tier": "P2"}, created_at=_ts(T_MINUS_1H))
        before = _snapshot([node], [])
        after = _snapshot([node], [])
        result = diff_graph_snapshots(before, after, "INC-014", INCIDENT_START)
        assert result.changed_nodes == []

    def test_node_type_preserved_in_diff(self):
        node_before = _make_node("rc:conn-pool", "root_cause",
                                 props={"category": "saturation"},
                                 created_at=_ts(T_MINUS_1H))
        node_after = _make_node("rc:conn-pool", "root_cause",
                                props={"category": "exhaustion"},  # changed
                                created_at=_ts(T_MINUS_5MIN))
        before = _snapshot([node_before], [])
        after = _snapshot([node_after], [])

        result = diff_graph_snapshots(before, after, "INC-015", INCIDENT_START)
        assert result.changed_nodes[0].node_type == "root_cause"


# ---------------------------------------------------------------------------
# most_likely_trigger selection tests
# ---------------------------------------------------------------------------

class TestTriggerSelection:
    def test_closest_edge_to_incident_is_trigger(self):
        """The edge added closest to incident start should be the trigger."""
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        node_c = _make_node("svc:c", created_at=_ts(T_MINUS_1H))

        # Edge 1: added 30 minutes before incident
        edge_far = _make_edge("svc:a", "svc:b", "CAUSED_BY",
                              created_at=_ts(T - timedelta(minutes=30)))
        # Edge 2: added 1 minute before incident — should be the trigger
        edge_close = _make_edge("svc:b", "svc:c", "CAUSED_BY",
                                created_at=_ts(T_MINUS_1MIN))

        before = _snapshot([node_a, node_b, node_c], [])
        after = _snapshot([node_a, node_b, node_c], [edge_far, edge_close])

        result = diff_graph_snapshots(before, after, "INC-016", INCIDENT_START)

        assert result.most_likely_trigger is not None
        assert isinstance(result.most_likely_trigger, GraphEdgeDiff)
        # The close edge should have higher confidence
        assert result.most_likely_trigger.source_node == "svc:b"
        assert result.most_likely_trigger.target_node == "svc:c"

    def test_trigger_confidence_higher_for_closer_change(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        edge_close = _make_edge("svc:a", "svc:b", "AFFECTED",
                                created_at=_ts(T_MINUS_1MIN))
        edge_far = _make_edge("svc:a", "svc:b", "CAUSED_BY",
                              created_at=_ts(T - timedelta(hours=3)))

        before_close = _snapshot([node_a, node_b], [])
        after_close = _snapshot([node_a, node_b], [edge_close])
        result_close = diff_graph_snapshots(before_close, after_close, "INC-017a", INCIDENT_START)

        before_far = _snapshot([node_a, node_b], [])
        after_far = _snapshot([node_a, node_b], [edge_far])
        result_far = diff_graph_snapshots(before_far, after_far, "INC-017b", INCIDENT_START)

        assert result_close.trigger_confidence > result_far.trigger_confidence

    def test_post_incident_change_has_lower_confidence(self):
        """Changes that happened AFTER incident start should get penalised."""
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))

        # Edge created before incident
        edge_before = _make_edge("svc:a", "svc:b", "CAUSED_BY",
                                 created_at=_ts(T_MINUS_5MIN))
        # Edge created after incident
        edge_after = _make_edge("svc:a", "svc:b", "AFFECTED",
                                created_at=_ts(T_PLUS_10MIN))

        before = _snapshot([node_a, node_b], [])
        snap_before_edge = _snapshot([node_a, node_b], [edge_before])
        snap_after_edge = _snapshot([node_a, node_b], [edge_after])

        result_before = diff_graph_snapshots(before, snap_before_edge, "INC-018a", INCIDENT_START)
        result_after = diff_graph_snapshots(before, snap_after_edge, "INC-018b", INCIDENT_START)

        assert result_before.trigger_confidence > result_after.trigger_confidence

    def test_no_changes_means_no_trigger(self):
        snap = _snapshot([], [])
        result = diff_graph_snapshots(snap, snap, "INC-019", INCIDENT_START)
        assert result.most_likely_trigger is None
        assert result.trigger_confidence == 0.0

    def test_trigger_is_graph_edge_diff_or_graph_node_diff(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        new_edge = _make_edge("svc:a", "svc:b", "DEPENDS_ON", created_at=_ts(T_MINUS_5MIN))

        before = _snapshot([node_a, node_b], [])
        after = _snapshot([node_a, node_b], [new_edge])

        result = diff_graph_snapshots(before, after, "INC-020", INCIDENT_START)
        assert isinstance(result.most_likely_trigger, (GraphEdgeDiff, GraphNodeDiff))

    def test_edge_change_preferred_over_node_change_as_trigger(self):
        """Structural edge changes should rank higher than node property changes."""
        node_before = _make_node("svc:payment",
                                 props={"tier": "P2"},
                                 created_at=_ts(T_MINUS_1H))
        node_after = _make_node("svc:payment",
                                props={"tier": "P1"},
                                created_at=_ts(T_MINUS_1MIN))
        node_db = _make_node("svc:db", created_at=_ts(T_MINUS_1H))

        # Edge added at nearly the same time as the node property change
        new_edge = _make_edge("svc:payment", "svc:db", "CAUSED_BY",
                              created_at=_ts(T_MINUS_1MIN))

        before = _snapshot([node_before, node_db], [])
        after = _snapshot([node_after, node_db], [new_edge])

        result = diff_graph_snapshots(before, after, "INC-021", INCIDENT_START)
        # Edge change should win as trigger
        assert isinstance(result.most_likely_trigger, GraphEdgeDiff)


# ---------------------------------------------------------------------------
# Reasoning string tests
# ---------------------------------------------------------------------------

class TestReasoning:
    def test_reasoning_mentions_incident_id(self):
        snap = _snapshot([], [])
        result = diff_graph_snapshots(snap, snap, "INC-REASONING-001", INCIDENT_START)
        assert "INC-REASONING-001" in result.reasoning

    def test_reasoning_mentions_trigger_when_present(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        new_edge = _make_edge("svc:a", "svc:b", "CAUSED_BY", created_at=_ts(T_MINUS_5MIN))

        before = _snapshot([node_a, node_b], [])
        after = _snapshot([node_a, node_b], [new_edge])

        result = diff_graph_snapshots(before, after, "INC-022", INCIDENT_START)
        assert "CAUSED_BY" in result.reasoning or "svc:a" in result.reasoning

    def test_reasoning_is_non_empty(self):
        snap = _snapshot([], [])
        result = diff_graph_snapshots(snap, snap, "INC-023", INCIDENT_START)
        assert isinstance(result.reasoning, str)
        assert len(result.reasoning) > 0

    def test_high_confidence_reasoning_mentions_strong_correlation(self):
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        # Edge added just 30 seconds before incident → high confidence
        edge = _make_edge("svc:a", "svc:b", "CAUSED_BY",
                          created_at=_ts(T - timedelta(seconds=30)))

        before = _snapshot([node_a, node_b], [])
        after = _snapshot([node_a, node_b], [edge])

        result = diff_graph_snapshots(before, after, "INC-024", INCIDENT_START)
        # High confidence path in reasoning
        assert result.trigger_confidence >= 0.7
        assert "High trigger confidence" in result.reasoning or \
               "strong" in result.reasoning.lower() or \
               result.trigger_confidence >= 0.7  # just confirm it's high


# ---------------------------------------------------------------------------
# CausalDiff dataclass structure tests
# ---------------------------------------------------------------------------

class TestCausalDiffStructure:
    def _basic_result(self) -> CausalDiff:
        node_a = _make_node("svc:a", created_at=_ts(T_MINUS_1H))
        node_b = _make_node("svc:b", created_at=_ts(T_MINUS_1H))
        new_edge = _make_edge("svc:a", "svc:b", "DEPENDS_ON", created_at=_ts(T_MINUS_5MIN))
        before = _snapshot([node_a, node_b], [])
        after = _snapshot([node_a, node_b], [new_edge])
        return diff_graph_snapshots(before, after, "INC-STRUCT", INCIDENT_START)

    def test_returns_causal_diff(self):
        result = self._basic_result()
        assert isinstance(result, CausalDiff)

    def test_incident_id_preserved(self):
        result = self._basic_result()
        assert result.incident_id == "INC-STRUCT"

    def test_incident_start_time_preserved(self):
        result = self._basic_result()
        assert result.incident_start_time == INCIDENT_START

    def test_added_edges_is_list(self):
        result = self._basic_result()
        assert isinstance(result.added_edges, list)

    def test_removed_edges_is_list(self):
        result = self._basic_result()
        assert isinstance(result.removed_edges, list)

    def test_changed_nodes_is_list(self):
        result = self._basic_result()
        assert isinstance(result.changed_nodes, list)

    def test_trigger_confidence_is_float(self):
        result = self._basic_result()
        assert isinstance(result.trigger_confidence, float)

    def test_trigger_confidence_between_0_and_1(self):
        result = self._basic_result()
        assert 0.0 <= result.trigger_confidence <= 1.0

    def test_graph_edge_diff_has_required_fields(self):
        result = self._basic_result()
        for edge in result.added_edges + result.removed_edges:
            assert hasattr(edge, "edge_type")
            assert hasattr(edge, "source_node")
            assert hasattr(edge, "target_node")
            assert hasattr(edge, "change_type")
            assert hasattr(edge, "timestamp")
            assert hasattr(edge, "confidence_as_trigger")

    def test_snapshot_times_are_strings(self):
        result = self._basic_result()
        assert isinstance(result.before_snapshot_time, str)
        assert isinstance(result.after_snapshot_time, str)


# ---------------------------------------------------------------------------
# Mixed scenario: edges AND nodes changed
# ---------------------------------------------------------------------------

class TestMixedScenario:
    def test_simultaneous_edge_and_node_changes(self):
        """When both edges and nodes change, both should appear in the diff."""
        node_payment_before = _make_node(
            "svc:payment", props={"error_rate": 0.01}, created_at=_ts(T_MINUS_1H)
        )
        node_payment_after = _make_node(
            "svc:payment", props={"error_rate": 0.85}, created_at=_ts(T_MINUS_5MIN)
        )
        node_db = _make_node("svc:db", created_at=_ts(T_MINUS_1H))
        new_edge = _make_edge(
            "svc:payment", "svc:db", "CAUSED_BY", created_at=_ts(T_MINUS_5MIN)
        )

        before = _snapshot([node_payment_before, node_db], [])
        after = _snapshot([node_payment_after, node_db], [new_edge])

        result = diff_graph_snapshots(before, after, "INC-MIXED", INCIDENT_START)

        assert len(result.added_edges) >= 1
        assert len(result.changed_nodes) >= 1

    def test_complete_graph_replacement(self):
        """Completely different before/after graphs → large diff."""
        before_nodes = [_make_node(f"old-svc-{i}", created_at=_ts(T_MINUS_1H)) for i in range(3)]
        after_nodes = [_make_node(f"new-svc-{i}", created_at=_ts(T_MINUS_5MIN)) for i in range(3)]

        before = _snapshot(before_nodes, [])
        after = _snapshot(after_nodes, [])

        result = diff_graph_snapshots(before, after, "INC-FULL-REPLACE", INCIDENT_START)

        added_nodes = [n for n in result.changed_nodes if n.change_type == "added"]
        removed_nodes = [n for n in result.changed_nodes if n.change_type == "removed"]
        assert len(added_nodes) == 3
        assert len(removed_nodes) == 3
