"""Causal Graph Diff — structural diff of KG snapshots around an incident.

Instead of asking "what is broken?", this module asks "what CHANGED in the
dependency graph that could have caused this?".  This is the temporal graph
intelligence layer: compare a before-snapshot (T-1h) against an after-snapshot
(T+10min) to surface structural changes that correlate with the incident.

No competitor does this.  Traditional RCA tools inspect the current state;
SentinalAI inspects the *delta* — the set of edges added, removed, or modified
closest to the incident's start time.  The change nearest in time to
incident_start is the most likely trigger.

Snapshot format (matches KnowledgeGraph.to_dict() output):
    {
        "nodes": [
            {
                "node_id": "svc:payment-service",
                "node_type": "service",
                "label": "payment-service",
                "props": {"tier": "P1", "error_rate": 0.02},
                "created_at": 1713600000.0,
            },
            ...
        ],
        "edges": [
            {
                "edge_id": "inc-001::CAUSED_BY::svc:payment-db",
                "src_id": "inc-001",
                "dst_id": "svc:payment-db",
                "rel_type": "CAUSED_BY",
                "weight": 0.9,
                "props": {},
                "created_at": 1713600120.0,
            },
            ...
        ],
    }

The incident_start_time and snapshot timestamps are ISO-8601 strings.  All
temporal proximity calculations convert them to Unix epoch seconds internally.

Algorithm:
  1. Index nodes and edges by their stable keys in each snapshot.
  2. Compute three-way set difference:
       added   = keys in after  but not before
       removed = keys in before but not after
       changed = keys in both  but with differing properties / weight
  3. Score each change by temporal proximity to incident_start_time:
       proximity_score = 1 / (1 + |Δt|)  where Δt is seconds before incident
       Changes *after* incident start receive a small penalty (they are likely
       consequences, not causes).
  4. Select the change with the highest proximity_score as most_likely_trigger.
  5. Build a human-readable reasoning string.

Usage:
    from supervisor.causal_graph_diff import diff_graph_snapshots

    causal = diff_graph_snapshots(
        before=kg_snapshot_t_minus_1h,
        after=kg_snapshot_t_plus_10min,
        incident_id="INC-2026-001",
        incident_start_time="2026-04-19T14:00:00Z",
    )

    print(causal.most_likely_trigger)
    print(causal.reasoning)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.causal_graph_diff")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GraphEdgeDiff:
    """Describes a single edge that was added, removed, or modified."""

    edge_type: str           # "AFFECTED", "CAUSED_BY", "DEPENDS_ON", etc.
    source_node: str
    target_node: str
    change_type: str         # "added" | "removed" | "modified"
    timestamp: str           # ISO-8601 string when this change appeared in the graph
    confidence_as_trigger: float  # 0.0–1.0; higher = more likely to be the root trigger


@dataclass
class GraphNodeDiff:
    """Describes a node that was added, removed, or had properties changed."""

    node_id: str
    node_type: str
    change_type: str         # "added" | "removed" | "property_changed"
    changed_properties: dict  # {property_name: (old_value, new_value)}
    timestamp: str           # ISO-8601 string


@dataclass
class CausalDiff:
    """Full structural diff of two KG snapshots around an incident."""

    incident_id: str
    incident_start_time: str          # ISO-8601
    before_snapshot_time: str         # ISO-8601
    after_snapshot_time: str          # ISO-8601
    added_edges: list[GraphEdgeDiff]
    removed_edges: list[GraphEdgeDiff]
    changed_nodes: list[GraphNodeDiff]
    most_likely_trigger: GraphEdgeDiff | GraphNodeDiff | None
    trigger_confidence: float         # confidence_as_trigger of the top candidate
    reasoning: str


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def diff_graph_snapshots(
    before: dict[str, Any],   # KG snapshot: {"nodes": [...], "edges": [...]}
    after: dict[str, Any],    # KG snapshot: {"nodes": [...], "edges": [...]}
    incident_id: str,
    incident_start_time: str,
) -> CausalDiff:
    """Find what changed in the dependency graph around incident time.

    Parameters
    ----------
    before:
        KG snapshot taken before the incident (e.g. T-1h).  Must have
        ``nodes`` and ``edges`` keys matching KnowledgeGraph.to_dict() format.
    after:
        KG snapshot taken after the incident started (e.g. T+10min).
    incident_id:
        Identifier for the incident (used in the CausalDiff output only).
    incident_start_time:
        ISO-8601 string for the moment the incident was declared.  Used to
        rank changes by temporal proximity.

    Returns
    -------
    CausalDiff
        Structural diff with all added/removed edges, changed nodes, and the
        most likely trigger with a confidence score and reasoning string.
    """
    # ------------------------------------------------------------------
    # Parse and infer snapshot timestamps
    # ------------------------------------------------------------------
    incident_ts = _parse_iso(incident_start_time)
    before_ts_str, after_ts_str = _infer_snapshot_times(before, after, incident_start_time)

    # ------------------------------------------------------------------
    # Index nodes and edges from each snapshot
    # ------------------------------------------------------------------
    before_nodes = _index_nodes(before.get("nodes", []))
    after_nodes = _index_nodes(after.get("nodes", []))
    before_edges = _index_edges(before.get("edges", []))
    after_edges = _index_edges(after.get("edges", []))

    # ------------------------------------------------------------------
    # Diff edges
    # ------------------------------------------------------------------
    added_edges: list[GraphEdgeDiff] = []
    removed_edges: list[GraphEdgeDiff] = []

    added_edge_keys = set(after_edges) - set(before_edges)
    removed_edge_keys = set(before_edges) - set(after_edges)

    for key in added_edge_keys:
        edge = after_edges[key]
        ts = _epoch_to_iso(edge.get("created_at", 0.0))
        confidence = _proximity_confidence(edge.get("created_at", 0.0), incident_ts)
        added_edges.append(GraphEdgeDiff(
            edge_type=edge.get("rel_type", ""),
            source_node=edge.get("src_id", ""),
            target_node=edge.get("dst_id", ""),
            change_type="added",
            timestamp=ts,
            confidence_as_trigger=confidence,
        ))

    for key in removed_edge_keys:
        edge = before_edges[key]
        # A removed edge's timestamp is the last-seen time (before snapshot time)
        ts = _epoch_to_iso(edge.get("created_at", 0.0))
        confidence = _proximity_confidence(edge.get("created_at", 0.0), incident_ts)
        removed_edges.append(GraphEdgeDiff(
            edge_type=edge.get("rel_type", ""),
            source_node=edge.get("src_id", ""),
            target_node=edge.get("dst_id", ""),
            change_type="removed",
            timestamp=ts,
            confidence_as_trigger=confidence,
        ))

    # Also detect edges whose weight / props changed (in both snapshots)
    common_edge_keys = set(before_edges) & set(after_edges)
    for key in common_edge_keys:
        b_edge = before_edges[key]
        a_edge = after_edges[key]
        if _edge_changed(b_edge, a_edge):
            ts = _epoch_to_iso(a_edge.get("created_at", 0.0))
            confidence = _proximity_confidence(a_edge.get("created_at", 0.0), incident_ts)
            added_edges.append(GraphEdgeDiff(
                edge_type=a_edge.get("rel_type", ""),
                source_node=a_edge.get("src_id", ""),
                target_node=a_edge.get("dst_id", ""),
                change_type="modified",
                timestamp=ts,
                confidence_as_trigger=confidence,
            ))

    # ------------------------------------------------------------------
    # Diff nodes
    # ------------------------------------------------------------------
    changed_nodes: list[GraphNodeDiff] = []

    added_node_keys = set(after_nodes) - set(before_nodes)
    removed_node_keys = set(before_nodes) - set(after_nodes)
    common_node_keys = set(before_nodes) & set(after_nodes)

    for key in added_node_keys:
        node = after_nodes[key]
        ts = _epoch_to_iso(node.get("created_at", 0.0))
        changed_nodes.append(GraphNodeDiff(
            node_id=node.get("node_id", key),
            node_type=node.get("node_type", ""),
            change_type="added",
            changed_properties={},
            timestamp=ts,
        ))

    for key in removed_node_keys:
        node = before_nodes[key]
        ts = _epoch_to_iso(node.get("created_at", 0.0))
        changed_nodes.append(GraphNodeDiff(
            node_id=node.get("node_id", key),
            node_type=node.get("node_type", ""),
            change_type="removed",
            changed_properties={},
            timestamp=ts,
        ))

    for key in common_node_keys:
        b_node = before_nodes[key]
        a_node = after_nodes[key]
        prop_diffs = _diff_props(b_node.get("props", {}), a_node.get("props", {}))
        if prop_diffs:
            ts = _epoch_to_iso(a_node.get("created_at", 0.0))
            changed_nodes.append(GraphNodeDiff(
                node_id=a_node.get("node_id", key),
                node_type=a_node.get("node_type", ""),
                change_type="property_changed",
                changed_properties=prop_diffs,
                timestamp=ts,
            ))

    # ------------------------------------------------------------------
    # Rank and select most likely trigger
    # ------------------------------------------------------------------
    most_likely_trigger, trigger_confidence = _select_trigger(
        added_edges, removed_edges, changed_nodes, incident_ts
    )

    # ------------------------------------------------------------------
    # Build reasoning
    # ------------------------------------------------------------------
    reasoning = _build_reasoning(
        incident_id,
        incident_start_time,
        added_edges,
        removed_edges,
        changed_nodes,
        most_likely_trigger,
        trigger_confidence,
    )

    logger.info(
        "Causal graph diff: incident=%s added_edges=%d removed_edges=%d "
        "changed_nodes=%d trigger_confidence=%.2f",
        incident_id,
        len(added_edges),
        len(removed_edges),
        len(changed_nodes),
        trigger_confidence,
    )

    return CausalDiff(
        incident_id=incident_id,
        incident_start_time=incident_start_time,
        before_snapshot_time=before_ts_str,
        after_snapshot_time=after_ts_str,
        added_edges=added_edges,
        removed_edges=removed_edges,
        changed_nodes=changed_nodes,
        most_likely_trigger=most_likely_trigger,
        trigger_confidence=trigger_confidence,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _index_nodes(nodes: list[dict]) -> dict[str, dict]:
    """Index nodes by node_id."""
    return {n["node_id"]: n for n in nodes if "node_id" in n}


def _index_edges(edges: list[dict]) -> dict[str, dict]:
    """Index edges by a stable composite key: src::rel::dst."""
    indexed: dict[str, dict] = {}
    for e in edges:
        src = e.get("src_id", "")
        dst = e.get("dst_id", "")
        rel = e.get("rel_type", "")
        if src and dst and rel:
            key = f"{src}::{rel}::{dst}"
            indexed[key] = e
    return indexed


def _edge_changed(before: dict, after: dict) -> bool:
    """Return True if any meaningful property of the edge changed."""
    if abs(before.get("weight", 1.0) - after.get("weight", 1.0)) > 1e-6:
        return True
    before_props = before.get("props", {})
    after_props = after.get("props", {})
    return before_props != after_props


def _diff_props(before_props: dict, after_props: dict) -> dict:
    """Return {key: (old_value, new_value)} for properties that changed."""
    diffs: dict[str, tuple] = {}
    all_keys = set(before_props) | set(after_props)
    for key in all_keys:
        old_val = before_props.get(key)
        new_val = after_props.get(key)
        if old_val != new_val:
            diffs[key] = (old_val, new_val)
    return diffs


def _parse_iso(ts_str: str) -> float:
    """Parse an ISO-8601 timestamp string into a Unix epoch float."""
    if not ts_str:
        return 0.0
    # Handle both "Z" suffix and "+00:00" offset
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        logger.warning("Could not parse timestamp: %s", ts_str)
        return 0.0


def _epoch_to_iso(epoch: float) -> str:
    """Convert a Unix epoch float to an ISO-8601 string (UTC)."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _proximity_confidence(change_ts: float, incident_ts: float) -> float:
    """Score how likely a change is to be the trigger based on timing.

    A change that occurred just before the incident start gets a high score
    (close to 1.0).  Changes far in the past, or changes that happened after
    the incident started, get lower scores.

    Formula:
        raw_score = 1 / (1 + |Δt_seconds|)
        If the change happened AFTER incident_start, apply a 50% penalty
        (it is more likely a symptom than a cause).
    """
    if not change_ts or not incident_ts:
        return 0.0

    delta = incident_ts - change_ts  # positive = change happened before incident
    abs_delta = abs(delta)

    # Base proximity: closer in time → higher score
    # We use a half-life of 300s (5 min) to normalise the decay
    proximity = 1.0 / (1.0 + abs_delta / 300.0)

    # Penalty for post-incident changes (likely consequences, not causes)
    if delta < 0:
        proximity *= 0.5

    return round(min(proximity, 1.0), 4)


def _select_trigger(
    added_edges: list[GraphEdgeDiff],
    removed_edges: list[GraphEdgeDiff],
    changed_nodes: list[GraphNodeDiff],
    incident_ts: float,
) -> tuple[GraphEdgeDiff | GraphNodeDiff | None, float]:
    """Select the single most likely trigger from all graph changes.

    Edge changes (structural) are preferred over node property changes because
    new dependency edges or broken edges are more directly causal.  Within each
    category, we sort by confidence_as_trigger descending.

    Returns (trigger, confidence) where confidence is the winning score.
    """
    # Collect all scored candidates
    candidates: list[tuple[float, GraphEdgeDiff | GraphNodeDiff]] = []

    for edge in added_edges + removed_edges:
        candidates.append((edge.confidence_as_trigger, edge))

    for node in changed_nodes:
        # Node changes have a slight inherent penalty vs structural edge changes
        node_confidence = _proximity_confidence(
            _parse_iso(node.timestamp), incident_ts
        ) * 0.8
        candidates.append((node_confidence, node))

    if not candidates:
        return None, 0.0

    # Sort descending by confidence; use change_type as a tiebreaker
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_candidate = candidates[0]

    return best_candidate, round(best_score, 4)


def _infer_snapshot_times(
    before: dict[str, Any],
    after: dict[str, Any],
    incident_start_time: str,
) -> tuple[str, str]:
    """Infer snapshot timestamps from snapshot metadata or node created_at times."""
    def _latest_ts(snapshot: dict) -> str:
        nodes = snapshot.get("nodes", [])
        edges = snapshot.get("edges", [])
        all_ts = [n.get("created_at", 0.0) for n in nodes] + \
                 [e.get("created_at", 0.0) for e in edges]
        all_ts = [t for t in all_ts if t]
        if not all_ts:
            return ""
        return _epoch_to_iso(max(all_ts))

    # Prefer explicit snapshot_time metadata; fall back to latest entity timestamp
    before_time = (
        before.get("snapshot_time")
        or before.get("captured_at")
        or _latest_ts(before)
        or incident_start_time
    )
    after_time = (
        after.get("snapshot_time")
        or after.get("captured_at")
        or _latest_ts(after)
        or incident_start_time
    )

    return str(before_time), str(after_time)


def _build_reasoning(
    incident_id: str,
    incident_start_time: str,
    added_edges: list[GraphEdgeDiff],
    removed_edges: list[GraphEdgeDiff],
    changed_nodes: list[GraphNodeDiff],
    most_likely_trigger: GraphEdgeDiff | GraphNodeDiff | None,
    trigger_confidence: float,
) -> str:
    """Build a human-readable reasoning string for the CausalDiff."""
    total_changes = len(added_edges) + len(removed_edges) + len(changed_nodes)

    if total_changes == 0:
        return (
            f"No structural changes detected in the knowledge graph between the "
            f"before and after snapshots for incident {incident_id}.  The incident "
            f"may have been caused by a transient load spike or a change not yet "
            f"captured in the topology."
        )

    lines: list[str] = [
        f"Graph diff for incident {incident_id} (start: {incident_start_time}) "
        f"found {total_changes} change(s): "
        f"{len(added_edges)} edge addition(s)/modification(s), "
        f"{len(removed_edges)} edge removal(s), "
        f"{len(changed_nodes)} node change(s)."
    ]

    if most_likely_trigger is None:
        lines.append("No trigger candidate could be identified.")
        return " ".join(lines)

    if isinstance(most_likely_trigger, GraphEdgeDiff):
        trigger = most_likely_trigger
        lines.append(
            f"Most likely trigger (confidence={trigger_confidence:.2f}): "
            f"Edge '{trigger.edge_type}' {trigger.change_type} between "
            f"'{trigger.source_node}' → '{trigger.target_node}' "
            f"at {trigger.timestamp}."
        )
    else:
        trigger = most_likely_trigger
        lines.append(
            f"Most likely trigger (confidence={trigger_confidence:.2f}): "
            f"Node '{trigger.node_id}' ({trigger.node_type}) {trigger.change_type} "
            f"at {trigger.timestamp}."
        )
        if trigger.changed_properties:
            prop_summary = ", ".join(
                f"{k}: {v[0]!r} → {v[1]!r}"
                for k, v in list(trigger.changed_properties.items())[:3]
            )
            lines.append(f"Changed properties: {prop_summary}.")

    if trigger_confidence < 0.3:
        lines.append(
            "Low trigger confidence — the most suspicious change is weakly correlated "
            "with the incident start time.  Manual review recommended."
        )
    elif trigger_confidence >= 0.7:
        lines.append(
            "High trigger confidence — this change occurred very close to the "
            "incident start and is a strong causal candidate."
        )

    return " ".join(lines)
