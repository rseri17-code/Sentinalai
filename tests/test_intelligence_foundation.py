"""Intelligence Foundation — Phase 1 tests.

Covers:
  1.  EvidenceNode — deterministic ID, serialization round-trip
  2.  EvidenceEdge — deterministic ID, serialization round-trip
  3.  EvidenceGraph — add/get O(1), node/edge count
  4.  EvidenceGraph — rejects edge with missing node
  5.  EvidenceGraph — get_outgoing/get_incoming filtered by relationship
  6.  EvidenceGraph — traverse_from BFS respects max_hops
  7.  EvidenceGraph — get_timeline is time-ordered
  8.  EvidenceGraph — find_root_cause_candidates returns CAUSED_BY targets
  9.  EvidenceGraph — to_dict/from_dict round-trip (no data loss)
  10. EvidenceGraph — export_dot produces valid DOT syntax
  11. ResolutionOutcome — make, to_dict, from_dict round-trip
  12. OutcomeStore — record + load_for_service
  13. OutcomeStore — success_rate_for_action below min_samples returns None
  14. ServiceProfile — record_investigation accumulates counts
  15. ServiceProfile — record_resolution updates avg_mttr
  16. ServiceProfile — to_dict/from_dict round-trip
  17. ServiceProfileIndex — get creates new profile on first access
  18. PatternSignature — from_graph_structure deterministic ID
  19. PatternSignature — similarity Jaccard calculation
  20. PatternSignature — record_occurrence updates confidence
  21. PatternSignatureIndex — match returns nothing below threshold
  22. PatternSignatureIndex — match returns best candidate above threshold
  23. DecisionTrace — make populates why and supporting_evidence
  24. DecisionTrace — to_dict/from_dict round-trip preserves extras
  25. DecisionTraceLog — append + load round-trip
  26. ReplaySeed — make + to_dict/from_dict round-trip
  27. ReplaySeedStore — save + load round-trip
  28. InvestigationStore — save_graph + load_graph round-trip
  29. InvestigationStore — _append_index + find_by_service
  30. bridge — evidence_dict_to_graph creates correct node types
  31. bridge — CORRELATED edges created between nodes
  32. bridge — graph_to_evidence_dict restores keys
  33. bridge — empty evidence dict returns empty graph
  34. bridge — evidence with nested data extracts timestamp
  35. schema — new_id is deterministic (same inputs → same output)
  36. schema — ts_bucket collapses near-simultaneous timestamps
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_dir():
    return tempfile.mkdtemp()


def _make_graph(investigation_id="INV-001", incident_id="INC-001", service="payment-service"):
    from intelligence import EvidenceGraph
    return EvidenceGraph(
        investigation_id=investigation_id,
        incident_id=incident_id,
        service=service,
        incident_type="oomkill",
    )


def _make_node(investigation_id="INV-001", source_type="golden_signals", entity_id="payment-service"):
    from intelligence import EvidenceNode, NodeType, EntityType
    return EvidenceNode.make(
        source_type=source_type,
        node_type=NodeType.METRIC,
        entity_id=entity_id,
        content={"error_rate": 18.4},
        investigation_id=investigation_id,
        timestamp="2026-06-03T03:55:00Z",
        confidence=1.0,
    )


# ===========================================================================
# 1. EvidenceNode — deterministic ID
# ===========================================================================

def test_evidence_node_deterministic_id():
    from intelligence import EvidenceNode, NodeType
    n1 = EvidenceNode.make(
        source_type="logs", node_type=NodeType.LOG,
        entity_id="svc", content={}, investigation_id="INV",
        timestamp="2026-06-03T03:55:00Z",
    )
    n2 = EvidenceNode.make(
        source_type="logs", node_type=NodeType.LOG,
        entity_id="svc", content={}, investigation_id="INV",
        timestamp="2026-06-03T03:55:05Z",   # within same 10s bucket
    )
    assert n1.node_id == n2.node_id, "Nodes within same bucket must share ID"


def test_evidence_node_serialization_roundtrip():
    n = _make_node()
    restored = type(n).from_dict(n.to_dict())
    assert restored.node_id    == n.node_id
    assert restored.source_type == n.source_type
    assert restored.entity_id  == n.entity_id
    assert restored.confidence == n.confidence


# ===========================================================================
# 2. EvidenceEdge — deterministic ID
# ===========================================================================

def test_evidence_edge_deterministic_id():
    from intelligence import EvidenceEdge, EdgeRelationship
    e1 = EvidenceEdge.make("src1", "dst1", EdgeRelationship.CAUSED_BY, "INV")
    e2 = EvidenceEdge.make("src1", "dst1", EdgeRelationship.CAUSED_BY, "INV")
    assert e1.edge_id == e2.edge_id


def test_evidence_edge_different_rels_different_ids():
    from intelligence import EvidenceEdge, EdgeRelationship
    e1 = EvidenceEdge.make("src", "dst", EdgeRelationship.CAUSED_BY,  "INV")
    e2 = EvidenceEdge.make("src", "dst", EdgeRelationship.CORRELATED, "INV")
    assert e1.edge_id != e2.edge_id


def test_evidence_edge_serialization_roundtrip():
    from intelligence import EvidenceEdge, EdgeRelationship
    e = EvidenceEdge.make("s", "d", EdgeRelationship.AFFECTS, "INV", weight=0.8)
    restored = EvidenceEdge.from_dict(e.to_dict())
    assert restored.edge_id       == e.edge_id
    assert restored.relationship  == e.relationship
    assert restored.weight        == e.weight


# ===========================================================================
# 3-5. EvidenceGraph core operations
# ===========================================================================

def test_graph_add_get_o1():
    from intelligence import EvidenceGraph, NodeType
    g = _make_graph()
    n = _make_node()
    g.add_node(n)
    assert g.get_node(n.node_id) is n
    assert g.node_count() == 1


def test_graph_rejects_edge_with_missing_node():
    from intelligence import EvidenceEdge, EdgeRelationship
    g = _make_graph()
    edge = EvidenceEdge.make("missing-src", "missing-dst", EdgeRelationship.CAUSED_BY, "INV-001")
    with pytest.raises(ValueError, match="not in graph"):
        g.add_edge(edge)


def test_graph_get_outgoing_filtered_by_relationship():
    from intelligence import EvidenceEdge, EdgeRelationship, NodeType
    g = _make_graph()
    n1 = _make_node(source_type="logs",    entity_id="svc-a")
    n2 = _make_node(source_type="metrics", entity_id="svc-b")
    n3 = _make_node(source_type="events",  entity_id="svc-c")
    g.add_node(n1); g.add_node(n2); g.add_node(n3)

    e_caused = EvidenceEdge.make(n1.node_id, n2.node_id, EdgeRelationship.CAUSED_BY,  "INV-001")
    e_corr   = EvidenceEdge.make(n1.node_id, n3.node_id, EdgeRelationship.CORRELATED, "INV-001")
    g.add_edge(e_caused); g.add_edge(e_corr)

    caused_neighbors = g.get_outgoing(n1.node_id, EdgeRelationship.CAUSED_BY)
    assert len(caused_neighbors) == 1
    assert caused_neighbors[0].node_id == n2.node_id


# ===========================================================================
# 6. traverse_from BFS respects max_hops
# ===========================================================================

def test_graph_traverse_max_hops():
    from intelligence import EvidenceEdge, EdgeRelationship
    g = _make_graph()
    nodes = [_make_node(source_type=f"src{i}", entity_id=f"svc-{i}") for i in range(4)]
    for n in nodes:
        g.add_node(n)
    # Linear chain: 0→1→2→3
    for i in range(3):
        g.add_edge(EvidenceEdge.make(
            nodes[i].node_id, nodes[i+1].node_id,
            EdgeRelationship.PRECEDED, "INV-001",
        ))

    reached_2 = g.traverse_from(nodes[0].node_id, max_hops=2)
    assert len(reached_2) == 2   # node 1 and node 2 only
    assert all(n.node_id in (nodes[1].node_id, nodes[2].node_id) for n in reached_2)


# ===========================================================================
# 7. get_timeline is time-ordered
# ===========================================================================

def test_graph_timeline_is_sorted():
    from intelligence import EvidenceNode, NodeType
    g = _make_graph()
    timestamps = ["2026-06-03T04:00:00Z", "2026-06-03T03:00:00Z", "2026-06-03T05:00:00Z"]
    for i, ts in enumerate(timestamps):
        n = EvidenceNode.make(
            source_type=f"src{i}", node_type=NodeType.LOG,
            entity_id="svc", content={}, investigation_id="INV-001",
            timestamp=ts,
        )
        g.add_node(n)

    timeline = g.get_timeline()
    ts_values = [n.timestamp for n in timeline]
    assert ts_values == sorted(ts_values)


# ===========================================================================
# 8. find_root_cause_candidates
# ===========================================================================

def test_find_root_cause_candidates():
    from intelligence import EvidenceEdge, EdgeRelationship
    g = _make_graph()
    effect = _make_node(source_type="alerts",  entity_id="oom-kill")
    cause  = _make_node(source_type="metrics", entity_id="heap-leak")
    g.add_node(effect); g.add_node(cause)
    g.add_edge(EvidenceEdge.make(effect.node_id, cause.node_id, EdgeRelationship.CAUSED_BY, "INV-001"))

    candidates = g.find_root_cause_candidates()
    assert len(candidates) == 1
    assert candidates[0].node_id == cause.node_id


# ===========================================================================
# 9. EvidenceGraph to_dict / from_dict round-trip
# ===========================================================================

def test_graph_serialization_roundtrip():
    from intelligence import EvidenceEdge, EdgeRelationship
    g = _make_graph()
    n1 = _make_node(source_type="logs",    entity_id="svc-a")
    n2 = _make_node(source_type="metrics", entity_id="svc-b")
    g.add_node(n1); g.add_node(n2)
    g.add_edge(EvidenceEdge.make(n1.node_id, n2.node_id, EdgeRelationship.CORRELATED, "INV-001"))

    data = g.to_dict()
    restored = type(g).from_dict(data)

    assert restored.investigation_id == g.investigation_id
    assert restored.node_count()     == g.node_count()
    assert restored.edge_count()     == g.edge_count()
    assert restored.get_node(n1.node_id) is not None
    assert restored.get_node(n2.node_id) is not None


# ===========================================================================
# 10. export_dot
# ===========================================================================

def test_graph_export_dot():
    g = _make_graph()
    g.add_node(_make_node())
    dot = g.export_dot()
    assert dot.startswith("digraph")
    assert "rankdir" in dot


# ===========================================================================
# 11-13. ResolutionOutcome
# ===========================================================================

def test_resolution_outcome_make_and_roundtrip():
    from intelligence import ResolutionOutcome, ResolutionStatus
    o = ResolutionOutcome.make(
        investigation_id="INV-001",
        incident_id="INC-001",
        service_id="payment-service",
        executed_action="kubectl rollout restart deployment/payment-service",
        resolution_status=ResolutionStatus.SUCCESS,
        mttr_minutes=22.5,
        recommended_action="restart deployment",
        operator_feedback="worked first time",
    )
    restored = ResolutionOutcome.from_dict(o.to_dict())
    assert restored.outcome_id         == o.outcome_id
    assert restored.resolution_status  == ResolutionStatus.SUCCESS
    assert restored.mttr_minutes       == 22.5
    assert restored.operator_feedback  == "worked first time"


def test_outcome_store_record_and_load(tmp_path):
    from intelligence import ResolutionOutcome, ResolutionStatus, OutcomeStore
    store = OutcomeStore(str(tmp_path / "outcomes.jsonl"))
    o = ResolutionOutcome.make(
        investigation_id="INV-A",
        incident_id="INC-A",
        service_id="checkout",
        executed_action="restart",
        resolution_status=ResolutionStatus.SUCCESS,
    )
    store.record(o)
    loaded = store.load_for_service("checkout")
    assert len(loaded) == 1
    assert loaded[0].outcome_id == o.outcome_id


def test_outcome_store_success_rate_below_min_samples(tmp_path):
    from intelligence import ResolutionOutcome, ResolutionStatus, OutcomeStore
    store = OutcomeStore(str(tmp_path / "outcomes.jsonl"))
    o = ResolutionOutcome.make(
        investigation_id="INV-B", incident_id="INC-B",
        service_id="svc", executed_action="rollback",
        resolution_status=ResolutionStatus.SUCCESS,
    )
    store.record(o)
    # Only 1 sample, min is 3 by default
    rate = store.success_rate_for_action("rollback", min_samples=3)
    assert rate is None


# ===========================================================================
# 14-17. ServiceProfile
# ===========================================================================

def test_service_profile_record_investigation():
    from intelligence import ServiceProfile
    p = ServiceProfile.new("payment-service")
    p.record_investigation("INV-001", "oomkill", entities=["heap"], dependencies=["db"])
    p.record_investigation("INV-002", "oomkill", entities=["heap"])

    assert p.total_investigations == 2
    assert p.recurring_incident_types["oomkill"] == 2
    assert p.recurring_entities["heap"] == 2
    assert p.recurring_dependencies["db"] == 1


def test_service_profile_record_resolution_updates_mttr():
    from intelligence import ServiceProfile
    p = ServiceProfile.new("checkout")
    p.record_resolution("INV-001", "restart", 20.0, "SUCCESS")
    p.record_resolution("INV-002", "rollback", 30.0, "PARTIAL_SUCCESS")

    assert p.avg_mttr_minutes == pytest.approx(25.0, abs=0.1)
    assert len(p.resolution_history) == 2


def test_service_profile_roundtrip():
    from intelligence import ServiceProfile
    p = ServiceProfile.new("auth")
    p.record_investigation("INV-X", "timeout", entities=["gateway"])
    restored = ServiceProfile.from_dict(p.to_dict())
    assert restored.service_name == "auth"
    assert restored.recurring_incident_types == p.recurring_incident_types


def test_service_profile_index_creates_new_profile(tmp_path):
    from intelligence.service_profile import ServiceProfileIndex
    idx = ServiceProfileIndex(str(tmp_path / "profiles.json"))
    p = idx.get("brand-new-service")
    assert p.service_name == "brand-new-service"
    assert p.total_investigations == 0


# ===========================================================================
# 18-22. PatternSignature
# ===========================================================================

def test_pattern_signature_deterministic_id():
    from intelligence import PatternSignature
    p1 = PatternSignature.from_graph_structure(["metric", "log"], ["CAUSED_BY"], "oomkill")
    p2 = PatternSignature.from_graph_structure(["metric", "log"], ["CAUSED_BY"], "oomkill")
    assert p1.pattern_id == p2.pattern_id


def test_pattern_signature_similarity():
    from intelligence import PatternSignature
    p = PatternSignature.from_graph_structure(["metric", "log", "event"], ["CAUSED_BY", "PRECEDED"], "oomkill")
    # Identical structure → similarity 1.0
    assert p.similarity(["metric", "log", "event"], ["CAUSED_BY", "PRECEDED"]) == 1.0
    # No overlap → similarity 0.0
    assert p.similarity(["runbook"], ["GENERATED_BY"]) == 0.0


def test_pattern_signature_record_occurrence_updates_confidence():
    from intelligence import PatternSignature
    p = PatternSignature.from_graph_structure(["metric"], ["CAUSED_BY"], "oomkill")
    assert p.confidence == 0.0
    for _ in range(5):
        p.record_occurrence(resolution_status="SUCCESS", resolution_action="restart")
    assert p.success_rate == 1.0
    assert p.confidence > 0.0


def test_pattern_index_no_match_below_threshold(tmp_path):
    from intelligence.pattern_signature import PatternSignatureIndex
    idx = PatternSignatureIndex(str(tmp_path / "patterns.json"))
    # Empty index returns []
    matches = idx.match(["metric"], ["CAUSED_BY"], "oomkill")
    assert matches == []


def test_pattern_index_returns_best_match(tmp_path):
    from intelligence import PatternSignature
    from intelligence.pattern_signature import PatternSignatureIndex
    idx = PatternSignatureIndex(str(tmp_path / "patterns.json"))

    p = PatternSignature.from_graph_structure(["metric", "log"], ["CAUSED_BY", "CORRELATED"], "oomkill")
    for _ in range(5):  # surpass MIN_FREQUENCY=2
        p.record_occurrence("SUCCESS", "restart")
    idx.upsert(p)

    matches = idx.match(["metric", "log"], ["CAUSED_BY", "CORRELATED"], "oomkill")
    assert len(matches) >= 1
    assert matches[0].pattern_id == p.pattern_id


# ===========================================================================
# 23-25. DecisionTrace
# ===========================================================================

def test_decision_trace_make_populates_why():
    from intelligence import DecisionTrace
    dt = DecisionTrace.make(
        investigation_id="INV-001",
        decision_type="hypothesis",
        decision="memory_leak_connection_pool",
        why="JVM heap at 96% capacity. ConnectionPoolManager held 198/200 connections. "
            "Prior incident INC-2025-0891 had identical pattern with SUCCESS outcome.",
        confidence=0.91,
        supporting_evidence=[{"node_id": "abc123", "confidence": 0.95}],
        pattern_id="dead1234",
        pattern_frequency=7,
        pattern_success_rate=0.86,
        prior_occurrence_count=3,
        historical_success_rate=1.0,
        reasoning_path=["high heap usage", "connection saturation", "oom kill"],
    )
    assert dt.why != ""
    assert dt.pattern_id == "dead1234"
    assert dt.confidence == pytest.approx(0.91)
    assert len(dt.supporting_evidence) == 1


def test_decision_trace_roundtrip_preserves_extras():
    from intelligence import DecisionTrace
    dt = DecisionTrace.make("INV", "recommendation", "restart", "fast recovery", 0.8)
    d = dt.to_dict()
    d["custom_field"] = "custom_value"
    restored = DecisionTrace.from_dict(d)
    assert restored.extras.get("custom_field") == "custom_value"
    assert restored.trace_id == dt.trace_id


def test_decision_trace_log_append_and_load(tmp_path):
    from intelligence import DecisionTrace
    from intelligence.decision_trace import DecisionTraceLog
    log = DecisionTraceLog(str(tmp_path))
    dt = DecisionTrace.make("INV-LOG", "gate_verdict", "G3_WARN", "low citation coverage", 0.3)
    log.append(dt)
    loaded = log.load("INV-LOG")
    assert len(loaded) == 1
    assert loaded[0].trace_id == dt.trace_id


# ===========================================================================
# 26-27. ReplaySeed
# ===========================================================================

def test_replay_seed_make_and_roundtrip():
    from intelligence import ReplaySeed
    g = _make_graph()
    seed = ReplaySeed.make(
        investigation_id="INV-001",
        incident_id="INC-001",
        incident_snapshot={"summary": "OOM kill", "service": "payment-service"},
        evidence_graph_snapshot=g.to_dict(),
        tool_call_sequence=[{"tool": "log_worker", "action": "search_logs"}],
    )
    assert seed.aarc_compatible is True
    restored = ReplaySeed.from_dict(seed.to_dict())
    assert restored.seed_id            == seed.seed_id
    assert restored.replay_seed_id     == seed.seed_id
    assert restored.schema_version     == "1.0"
    assert len(restored.tool_call_sequence) == 1


def test_replay_seed_store_save_and_load(tmp_path):
    from intelligence import ReplaySeed
    from intelligence.replay_seed import ReplaySeedStore
    store = ReplaySeedStore(str(tmp_path))
    g = _make_graph()
    seed = ReplaySeed.make("INV-002", "INC-002", {}, g.to_dict())
    store.save(seed)
    loaded = store.load("INV-002")
    assert loaded is not None
    assert loaded.seed_id == seed.seed_id


# ===========================================================================
# 28-29. InvestigationStore
# ===========================================================================

def test_investigation_store_save_load_graph(tmp_path):
    from intelligence.investigation_store import InvestigationStore
    store = InvestigationStore(
        investigations_dir=str(tmp_path),
        outcomes_path=str(tmp_path / "outcomes.jsonl"),
        profiles_path=str(tmp_path / "profiles.json"),
        patterns_path=str(tmp_path / "patterns.json"),
    )
    g = _make_graph()
    g.add_node(_make_node())
    store.save_graph(g)

    loaded = store.load_graph("INV-001")
    assert loaded is not None
    assert loaded.investigation_id == "INV-001"
    assert loaded.node_count() == 1


def test_investigation_store_find_by_service(tmp_path):
    from intelligence.investigation_store import InvestigationStore
    store = InvestigationStore(str(tmp_path), str(tmp_path / "o.jsonl"),
                               str(tmp_path / "p.json"), str(tmp_path / "pat.json"))
    g1 = _make_graph("INV-A", service="payment-service")
    g2 = _make_graph("INV-B", service="auth-service")
    store.save_graph(g1)
    store.save_graph(g2)

    results = store.find_by_service("payment-service")
    assert len(results) == 1
    assert results[0].investigation_id == "INV-A"


# ===========================================================================
# 30-34. Bridge
# ===========================================================================

def test_bridge_creates_correct_node_types():
    from intelligence import evidence_dict_to_graph, NodeType
    evidence = {
        "golden_signals": {"error_rate": 18.4},
        "search_logs":    {"data": [{"message": "OOM killed"}]},
        "k8s_events":     {"items": [{"type": "Warning"}]},
    }
    graph = evidence_dict_to_graph(evidence, "INV-BRIDGE", service="payment-service")
    node_types = {n.source_type: n.node_type for n in graph.all_nodes()}

    assert node_types["golden_signals"] == NodeType.METRIC
    assert node_types["search_logs"]    == NodeType.LOG
    assert node_types["k8s_events"]     == NodeType.EVENT


def test_bridge_creates_correlated_edges():
    from intelligence import evidence_dict_to_graph, EdgeRelationship
    evidence = {"logs": {"data": []}, "metrics": {"cpu": 90}}
    graph = evidence_dict_to_graph(evidence, "INV-CORR", service="svc")

    correlated = [e for e in graph.all_edges() if e.relationship == EdgeRelationship.CORRELATED]
    assert len(correlated) >= 1


def test_bridge_graph_to_evidence_dict_restores_keys():
    from intelligence import evidence_dict_to_graph, graph_to_evidence_dict
    evidence = {"logs": {"data": []}, "metrics": {"cpu": 90}}
    graph = evidence_dict_to_graph(evidence, "INV-RT", service="svc")
    restored = graph_to_evidence_dict(graph)
    assert "logs" in restored
    assert "metrics" in restored


def test_bridge_empty_evidence_returns_empty_graph():
    from intelligence import evidence_dict_to_graph
    graph = evidence_dict_to_graph({}, "INV-EMPTY")
    assert graph.node_count() == 0
    assert graph.edge_count() == 0


def test_bridge_extracts_timestamp_from_nested_data():
    from intelligence import evidence_dict_to_graph
    evidence = {
        "logs": {
            "data": [{"timestamp": "2026-06-03T04:00:00Z", "message": "OOM"}]
        }
    }
    graph = evidence_dict_to_graph(evidence, "INV-TS", service="svc")
    assert graph.node_count() == 1
    node = graph.all_nodes()[0]
    assert "2026" in node.timestamp


# ===========================================================================
# 35-36. Schema utilities
# ===========================================================================

def test_new_id_is_deterministic():
    from intelligence import new_id
    id1 = new_id("source", "entity", "ts_bucket")
    id2 = new_id("source", "entity", "ts_bucket")
    assert id1 == id2
    assert len(id1) == 16


def test_ts_bucket_collapses_same_10s_window():
    from intelligence.schema import ts_bucket
    b1 = ts_bucket("2026-06-03T04:00:00Z")
    b2 = ts_bucket("2026-06-03T04:00:09Z")   # same 10s window
    b3 = ts_bucket("2026-06-03T04:00:10Z")   # next window
    assert b1 == b2
    assert b1 != b3
