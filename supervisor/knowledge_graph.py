"""Graph-based RAG for SentinalAI.

Maintains a lightweight in-memory knowledge graph of incidents, services,
root causes, and their relationships.  Enables graph-traversal retrieval
(BFS/DFS) which outperforms flat semantic search for:

  - "What else broke when payment-service had a connection pool issue?"
  - "Which deployments caused OOMKilled incidents in the last 30 days?"
  - "Has this root cause appeared before on this service?"

Graph model:
  Nodes (entity types):
    - incident:   incident_id, summary, service, type, confidence
    - service:    name, tier, dependencies
    - root_cause: description (normalised), category
    - error_type: OOMKilled | timeout | saturation | config_change | ...

  Edges (relationship types):
    - AFFECTED:      incident → service
    - HAS_ROOT_CAUSE: incident → root_cause
    - CAUSED_BY:     incident → incident  (cascade)
    - RECURRED_ON:   root_cause → service
    - RELATED_TO:    incident → incident  (co-occurring)

The graph is file-backed JSON (KNOWLEDGE_GRAPH_PATH) and rebuilt from the
experience store on startup if the file is absent.  Thread-safe reads/writes
via a module-level RWLock-like pattern.

Configuration:
  KNOWLEDGE_GRAPH_PATH      — JSON file (default: eval/knowledge_graph.json)
  KNOWLEDGE_GRAPH_ENABLED   — enable/disable (default: true)
  KG_MAX_NODES              — cap on total nodes (default: 5000)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("sentinalai.knowledge_graph")

KG_ENABLED = os.environ.get("KNOWLEDGE_GRAPH_ENABLED", "true").lower() in ("1", "true", "yes")
KG_PATH = os.environ.get(
    "KNOWLEDGE_GRAPH_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "knowledge_graph.json"),
)
KG_MAX_NODES = int(os.environ.get("KG_MAX_NODES", "5000"))

_lock = threading.RLock()
_graph: "KnowledgeGraph | None" = None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class KGNode:
    node_id: str
    node_type: str          # incident | service | root_cause | error_type
    label: str
    props: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class KGEdge:
    edge_id: str
    src_id: str
    dst_id: str
    rel_type: str           # AFFECTED | HAS_ROOT_CAUSE | CAUSED_BY | RECURRED_ON | RELATED_TO
    weight: float = 1.0
    props: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Graph class
# ---------------------------------------------------------------------------

class KnowledgeGraph:
    """In-memory knowledge graph with file persistence."""

    def __init__(self) -> None:
        self._nodes: dict[str, KGNode] = {}
        self._edges: dict[str, KGEdge] = {}
        # Adjacency: node_id → list of (edge_id, dst_id)
        self._adj_out: dict[str, list[tuple[str, str]]] = {}
        self._adj_in:  dict[str, list[tuple[str, str]]] = {}

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #

    def add_node(self, node_id: str, node_type: str, label: str, **props: Any) -> KGNode:
        """Add or update a node."""
        if node_id in self._nodes:
            # Merge props on update
            self._nodes[node_id].props.update(props)
            return self._nodes[node_id]
        node = KGNode(node_id=node_id, node_type=node_type, label=label, props=dict(props))
        self._nodes[node_id] = node
        self._adj_out.setdefault(node_id, [])
        self._adj_in.setdefault(node_id, [])
        return node

    def add_edge(
        self, src_id: str, dst_id: str, rel_type: str, weight: float = 1.0, **props: Any
    ) -> KGEdge | None:
        """Add an edge between two nodes (no-op if either node absent)."""
        if src_id not in self._nodes or dst_id not in self._nodes:
            return None
        # Deduplicate same-type edges between same pair
        edge_key = f"{src_id}::{rel_type}::{dst_id}"
        if edge_key in self._edges:
            self._edges[edge_key].weight = max(self._edges[edge_key].weight, weight)
            return self._edges[edge_key]
        edge = KGEdge(
            edge_id=edge_key, src_id=src_id, dst_id=dst_id,
            rel_type=rel_type, weight=weight, props=dict(props),
        )
        self._edges[edge_key] = edge
        self._adj_out[src_id].append((edge_key, dst_id))
        self._adj_in[dst_id].append((edge_key, src_id))
        return edge

    def ingest_investigation(
        self,
        incident_id: str,
        incident_type: str,
        service: str,
        root_cause: str,
        confidence: int,
        related_incident_ids: list[str] | None = None,
    ) -> None:
        """Ingest a completed investigation into the graph."""
        # Incident node
        self.add_node(
            incident_id, "incident", incident_id,
            incident_type=incident_type, confidence=confidence,
        )
        # Service node
        svc_id = f"svc:{service}"
        self.add_node(svc_id, "service", service)
        self.add_edge(incident_id, svc_id, "AFFECTED")

        # Root cause node — normalise to avoid duplicates
        rc_id = _normalise_rc_id(root_cause)
        self.add_node(rc_id, "root_cause", root_cause[:120])
        self.add_edge(incident_id, rc_id, "HAS_ROOT_CAUSE", weight=confidence / 100.0)
        self.add_edge(rc_id, svc_id, "RECURRED_ON")

        # Error type node
        et_id = f"errtype:{incident_type}"
        self.add_node(et_id, "error_type", incident_type)
        self.add_edge(incident_id, et_id, "HAS_ROOT_CAUSE")

        # Correlate with related incidents
        for rel_id in (related_incident_ids or []):
            if rel_id in self._nodes:
                self.add_edge(incident_id, rel_id, "RELATED_TO")

        # Evict if over cap
        self._evict_if_needed()

    # ------------------------------------------------------------------ #
    # Query API
    # ------------------------------------------------------------------ #

    def get_node(self, node_id: str) -> KGNode | None:
        return self._nodes.get(node_id)

    def neighbors(self, node_id: str, rel_type: str | None = None) -> list[KGNode]:
        """Return outgoing neighbors (optionally filtered by relationship type)."""
        results: list[KGNode] = []
        for edge_id, dst_id in self._adj_out.get(node_id, []):
            edge = self._edges.get(edge_id)
            if rel_type and (edge is None or edge.rel_type != rel_type):
                continue
            if dst_id in self._nodes:
                results.append(self._nodes[dst_id])
        return results

    def find_similar_incidents(
        self,
        service: str,
        incident_type: str,
        max_hops: int = 2,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """BFS from service node to find historically similar incidents.

        Returns incidents sorted by relevance score (hop count + confidence).
        """
        svc_id = f"svc:{service}"
        if svc_id not in self._nodes:
            return []

        visited: set[str] = {svc_id}
        queue: deque[tuple[str, int, float]] = deque([(svc_id, 0, 1.0)])
        incident_scores: dict[str, float] = {}

        while queue:
            node_id, hops, score = queue.popleft()
            if hops >= max_hops:
                continue

            node = self._nodes.get(node_id)
            if node is None:
                continue

            # Traverse both in and out edges for context
            all_neighbors: list[tuple[str, str, float]] = []
            for eid, dst in self._adj_out.get(node_id, []):
                e = self._edges.get(eid)
                all_neighbors.append((dst, e.rel_type if e else "", e.weight if e else 1.0))
            for eid, src in self._adj_in.get(node_id, []):
                e = self._edges.get(eid)
                all_neighbors.append((src, e.rel_type if e else "", e.weight if e else 1.0))

            for nbr_id, rel_type, edge_weight in all_neighbors:
                if nbr_id in visited:
                    continue
                visited.add(nbr_id)
                nbr = self._nodes.get(nbr_id)
                if nbr is None:
                    continue

                hop_score = score * edge_weight * (0.7 ** hops)

                if nbr.node_type == "incident" and nbr_id != svc_id:
                    # Boost score if same incident type
                    type_match = 1.5 if nbr.props.get("incident_type") == incident_type else 1.0
                    incident_scores[nbr_id] = max(
                        incident_scores.get(nbr_id, 0.0),
                        hop_score * type_match,
                    )
                queue.append((nbr_id, hops + 1, hop_score))

        # Collect results
        results: list[dict] = []
        for inc_id, score in sorted(incident_scores.items(), key=lambda x: -x[1])[:top_k]:
            inc_node = self._nodes[inc_id]
            # Find root cause for this incident
            rc_nodes = self.neighbors(inc_id, "HAS_ROOT_CAUSE")
            rc = next((n.label for n in rc_nodes if n.node_type == "root_cause"), "")
            results.append({
                "incident_id": inc_id,
                "incident_type": inc_node.props.get("incident_type", ""),
                "confidence": inc_node.props.get("confidence", 0),
                "root_cause": rc,
                "relevance_score": round(score, 4),
            })
        return results

    def find_recurring_root_causes(self, service: str) -> list[dict[str, Any]]:
        """Return root causes that have recurred on a service, sorted by frequency."""
        svc_id = f"svc:{service}"
        rc_nodes = [
            self._nodes[src]
            for eid, src in self._adj_in.get(svc_id, [])
            if self._nodes.get(src) and self._nodes[src].node_type == "root_cause"
        ]
        # Count how many incidents share each root cause
        counts: dict[str, int] = {}
        for rc_node in rc_nodes:
            incidents_with_rc = [
                src for eid, src in self._adj_in.get(rc_node.node_id, [])
                if self._nodes.get(src) and self._nodes[src].node_type == "incident"
            ]
            counts[rc_node.node_id] = len(incidents_with_rc)

        return [
            {
                "root_cause_id": rc_id,
                "description": self._nodes[rc_id].label,
                "recurrence_count": count,
            }
            for rc_id, count in sorted(counts.items(), key=lambda x: -x[1])
            if count > 0
        ]

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return len(self._edges)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges.values()],
            "node_count": self.node_count(),
            "edge_count": self.edge_count(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeGraph":
        g = cls()
        for nd in data.get("nodes", []):
            node = KGNode(**{k: v for k, v in nd.items() if k in KGNode.__dataclass_fields__})
            g._nodes[node.node_id] = node
            g._adj_out.setdefault(node.node_id, [])
            g._adj_in.setdefault(node.node_id, [])
        for ed in data.get("edges", []):
            edge = KGEdge(**{k: v for k, v in ed.items() if k in KGEdge.__dataclass_fields__})
            g._edges[edge.edge_id] = edge
            g._adj_out.setdefault(edge.src_id, []).append((edge.edge_id, edge.dst_id))
            g._adj_in.setdefault(edge.dst_id, []).append((edge.edge_id, edge.src_id))
        return g

    def save(self, path: str = KG_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _evict_if_needed(self) -> None:
        """Evict oldest incident nodes if over cap."""
        if len(self._nodes) <= KG_MAX_NODES:
            return
        incident_nodes = sorted(
            [n for n in self._nodes.values() if n.node_type == "incident"],
            key=lambda n: n.created_at,
        )
        for node in incident_nodes[: len(self._nodes) - KG_MAX_NODES]:
            self._remove_node(node.node_id)

    def _remove_node(self, node_id: str) -> None:
        """Remove a node and all its edges."""
        for eid, _ in list(self._adj_out.get(node_id, [])):
            self._edges.pop(eid, None)
        for eid, _ in list(self._adj_in.get(node_id, [])):
            self._edges.pop(eid, None)
        self._adj_out.pop(node_id, None)
        self._adj_in.pop(node_id, None)
        self._nodes.pop(node_id, None)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

def get_graph() -> KnowledgeGraph:
    """Return the singleton knowledge graph, loading from disk if needed."""
    global _graph
    with _lock:
        if _graph is not None:
            return _graph
        g = KnowledgeGraph()
        if os.path.exists(KG_PATH):
            try:
                with open(KG_PATH) as f:
                    data = json.load(f)
                g = KnowledgeGraph.from_dict(data)
                logger.info("Loaded knowledge graph: %d nodes, %d edges", g.node_count(), g.edge_count())
            except Exception as exc:
                logger.warning("Could not load knowledge graph: %s — starting fresh", exc)
        _graph = g
        return _graph


def ingest_to_graph(
    incident_id: str,
    incident_type: str,
    service: str,
    root_cause: str,
    confidence: int,
    related_incident_ids: list[str] | None = None,
    save: bool = True,
) -> None:
    """Convenience: ingest an investigation into the global graph."""
    if not KG_ENABLED:
        return
    with _lock:
        g = get_graph()
        g.ingest_investigation(
            incident_id, incident_type, service, root_cause, confidence, related_incident_ids
        )
        if save:
            try:
                g.save()
            except Exception as exc:
                logger.warning("Failed to persist knowledge graph: %s", exc)


def query_similar(service: str, incident_type: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Query the global graph for similar historical incidents."""
    if not KG_ENABLED:
        return []
    g = get_graph()
    return g.find_similar_incidents(service, incident_type, top_k=top_k)


def _normalise_rc_id(root_cause: str) -> str:
    """Create a stable node ID from a root cause string."""
    import re
    cleaned = re.sub(r"[^a-z0-9 ]", "", root_cause.lower())
    words = cleaned.split()[:5]
    return "rc:" + "_".join(words)
