"""Backward-compatibility bridge: existing evidence dict → EvidenceGraph.

Converts the flat evidence dict (as produced by SentinalAI workers) into
a typed EvidenceGraph without changing any existing code paths.

evidence_dict_to_graph() is the primary entry point.
All failures are non-fatal — returns empty graph on error.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from intelligence.evidence_graph import EvidenceEdge, EvidenceGraph, EvidenceNode
from intelligence.schema import (
    SOURCE_NODE_TYPE,
    EdgeRelationship,
    EntityType,
    InvestigationPhase,
    NodeType,
    new_id,
)

logger = logging.getLogger("sentinalai.intelligence.bridge")


def evidence_dict_to_graph(
    evidence: dict[str, Any],
    investigation_id: str,
    incident_id: str = "",
    service: str = "",
    incident_type: str = "",
    collected_at: str = "",
) -> EvidenceGraph:
    """Convert a flat evidence dict to an EvidenceGraph.

    Creates one node per evidence key that has non-empty data.
    Auto-creates CORRELATED edges between nodes from the same service.
    Does NOT infer CAUSED_BY — that requires analysis.
    """
    graph = EvidenceGraph(
        investigation_id=investigation_id,
        incident_id=incident_id,
        service=service,
        incident_type=incident_type,
        phase=InvestigationPhase.COLLECTING,
    )
    if not evidence:
        return graph

    ts_now = collected_at or datetime.now(timezone.utc).isoformat()
    created_nodes: list[str] = []

    for key, value in evidence.items():
        if not value:
            continue
        try:
            node = _make_node(
                key=key,
                value=value,
                investigation_id=investigation_id,
                service=service,
                collected_at=ts_now,
            )
            if node:
                graph.add_node(node)
                created_nodes.append(node.node_id)
        except Exception as exc:
            logger.debug("bridge: failed to convert key=%s: %s", key, exc)

    # Auto-wire CORRELATED edges for all nodes on the same service
    _add_correlation_edges(graph, created_nodes, investigation_id, service)

    return graph


def _make_node(
    key: str,
    value: Any,
    investigation_id: str,
    service: str,
    collected_at: str,
) -> EvidenceNode | None:
    node_type = _infer_node_type(key)
    content   = _extract_content(value)
    timestamp = _extract_timestamp(value) or collected_at
    confidence = _infer_confidence(key)

    return EvidenceNode.make(
        source_type=key,
        node_type=node_type,
        entity_id=service or key,
        content=content,
        investigation_id=investigation_id,
        timestamp=timestamp,
        collected_at=collected_at,
        confidence=confidence,
        entity_type=EntityType.SERVICE,
    )


def _infer_node_type(key: str) -> NodeType:
    """Map evidence key to NodeType using SOURCE_NODE_TYPE table + heuristics."""
    lowkey = key.lower()
    if lowkey in SOURCE_NODE_TYPE:
        return SOURCE_NODE_TYPE[lowkey]
    for prefix, nt in SOURCE_NODE_TYPE.items():
        if lowkey.startswith(prefix) or prefix in lowkey:
            return nt
    # Heuristic fallbacks
    if any(w in lowkey for w in ("log", "error", "warn", "exception")):
        return NodeType.LOG
    if any(w in lowkey for w in ("metric", "signal", "cpu", "memory", "rate")):
        return NodeType.METRIC
    if any(w in lowkey for w in ("event", "k8s", "pod", "restart")):
        return NodeType.EVENT
    if any(w in lowkey for w in ("change", "deploy", "release", "commit")):
        return NodeType.CHANGE
    if any(w in lowkey for w in ("trace", "span", "apm")):
        return NodeType.TRACE
    if any(w in lowkey for w in ("alert", "incident", "moogsoft", "pagerduty")):
        return NodeType.ALERT
    return NodeType.LOG  # safe default


def _extract_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value[:20]}   # cap at 20 items
    return {"raw": str(value)[:500]}


def _extract_timestamp(value: Any) -> str:
    """Try to find a timestamp in the evidence value."""
    if isinstance(value, dict):
        for field in ("timestamp", "collected_at", "created_at", "start_time", "ts"):
            ts = value.get(field)
            if ts and isinstance(ts, str):
                return ts
        # Nested: check first item in data array
        data = value.get("data") or value.get("items") or value.get("events")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                for field in ("timestamp", "ts", "_time", "time"):
                    ts = first.get(field)
                    if ts and isinstance(ts, str):
                        return ts
    return ""


def _infer_confidence(key: str) -> float:
    """Return base confidence for a source key using the same tiers as source_confidence.py."""
    try:
        from supervisor.retrieval.source_confidence import score_source
        sc = score_source(source_type=key, collected_at=None, age_hours=0)
        return sc.base_confidence
    except Exception:
        pass
    # Fallback table (no external dep)
    _TABLE = {
        "golden_signals": 1.0, "check_golden_signals": 1.0,
        "search_logs": 0.85, "k8s_events": 0.80,
        "get_change_data": 0.78, "experience_store": 0.70,
        "wiki_note": 0.60, "runbook": 0.58,
    }
    return _TABLE.get(key, 0.65)


def _add_correlation_edges(
    graph: EvidenceGraph,
    node_ids: list[str],
    investigation_id: str,
    service: str,
) -> None:
    """Wire CORRELATED edges between all nodes (same investigation = co-occurring evidence)."""
    for i, src_id in enumerate(node_ids):
        for dst_id in node_ids[i + 1:]:
            try:
                edge = EvidenceEdge.make(
                    src_id=src_id,
                    dst_id=dst_id,
                    relationship=EdgeRelationship.CORRELATED,
                    investigation_id=investigation_id,
                    weight=0.5,
                )
                graph.add_edge(edge)
            except Exception:
                pass  # skip if nodes missing


def graph_to_evidence_dict(graph: EvidenceGraph) -> dict[str, Any]:
    """Convert EvidenceGraph back to evidence dict for use by existing code."""
    result: dict[str, Any] = {}
    for node in graph.all_nodes():
        result[node.source_type] = node.content
    return result
