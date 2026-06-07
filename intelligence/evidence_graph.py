"""EvidenceNode, EvidenceEdge, EvidenceGraph — core investigation graph model.

EvidenceGraph is the single source of truth for one investigation.
Nodes are typed evidence artifacts; edges encode operational relationships.
IDs are deterministic (sha256) enabling idempotent re-ingestion.

Design constraints:
  - O(1) lookup by node_id or edge_id
  - Time-ordered traversal for timeline reconstruction
  - Full JSON round-trip (to_dict / from_dict)
  - Thread-safe for concurrent evidence collection
  - KG-ready: service_id / application_id / topology_id fields present
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.schema import (
    SCHEMA_VERSION,
    EdgeRelationship,
    EntityType,
    InvestigationPhase,
    NodeType,
    new_id,
    ts_bucket,
)


@dataclass
class EvidenceNode:
    node_id:          str
    node_type:        NodeType
    source_type:      str          # splunk | dynatrace | sysdig | servicenow | cmdb | moogsoft
    entity_id:        str          # logical entity: service name, host, pod
    entity_type:      EntityType
    content:          dict[str, Any]
    timestamp:        str          # ISO-8601: when evidence occurred
    collected_at:     str          # ISO-8601: when collected
    confidence:       float        # 0–1 from source_confidence tiers
    investigation_id: str
    # Knowledge Graph readiness — populated on CMDB integration
    service_id:       str = ""
    application_id:   str = ""
    topology_id:      str = ""
    agent_id:         str = ""

    @classmethod
    def make(
        cls,
        source_type: str,
        node_type: NodeType,
        entity_id: str,
        content: dict[str, Any],
        investigation_id: str,
        timestamp: str = "",
        collected_at: str = "",
        confidence: float = 0.65,
        entity_type: EntityType = EntityType.SERVICE,
        **kg_fields: str,
    ) -> "EvidenceNode":
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        node_id = new_id(source_type, entity_id, ts_bucket(ts))
        return cls(
            node_id=node_id,
            node_type=node_type,
            source_type=source_type,
            entity_id=entity_id,
            entity_type=entity_type,
            content=content,
            timestamp=ts,
            collected_at=collected_at or datetime.now(timezone.utc).isoformat(),
            confidence=confidence,
            investigation_id=investigation_id,
            service_id=kg_fields.get("service_id", ""),
            application_id=kg_fields.get("application_id", ""),
            topology_id=kg_fields.get("topology_id", ""),
            agent_id=kg_fields.get("agent_id", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "source_type": self.source_type,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type.value,
            "content": self.content,
            "timestamp": self.timestamp,
            "collected_at": self.collected_at,
            "confidence": round(self.confidence, 4),
            "investigation_id": self.investigation_id,
            "service_id": self.service_id,
            "application_id": self.application_id,
            "topology_id": self.topology_id,
            "agent_id": self.agent_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvidenceNode":
        return cls(
            node_id=d["node_id"],
            node_type=NodeType(d["node_type"]),
            source_type=d["source_type"],
            entity_id=d["entity_id"],
            entity_type=EntityType(d.get("entity_type", EntityType.UNKNOWN.value)),
            content=d.get("content", {}),
            timestamp=d.get("timestamp", ""),
            collected_at=d.get("collected_at", ""),
            confidence=float(d.get("confidence", 0.65)),
            investigation_id=d.get("investigation_id", ""),
            service_id=d.get("service_id", ""),
            application_id=d.get("application_id", ""),
            topology_id=d.get("topology_id", ""),
            agent_id=d.get("agent_id", ""),
        )


@dataclass
class EvidenceEdge:
    edge_id:          str
    src_id:           str
    dst_id:           str
    relationship:     EdgeRelationship
    weight:           float          # 0–1 edge confidence
    timestamp:        str
    investigation_id: str
    evidence:         dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        src_id: str,
        dst_id: str,
        relationship: EdgeRelationship,
        investigation_id: str,
        weight: float = 0.5,
        timestamp: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> "EvidenceEdge":
        edge_id = new_id(src_id, relationship.value, dst_id)
        return cls(
            edge_id=edge_id,
            src_id=src_id,
            dst_id=dst_id,
            relationship=relationship,
            weight=weight,
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            investigation_id=investigation_id,
            evidence=evidence or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "src_id": self.src_id,
            "dst_id": self.dst_id,
            "relationship": self.relationship.value,
            "weight": round(self.weight, 4),
            "timestamp": self.timestamp,
            "investigation_id": self.investigation_id,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvidenceEdge":
        return cls(
            edge_id=d["edge_id"],
            src_id=d["src_id"],
            dst_id=d["dst_id"],
            relationship=EdgeRelationship(d["relationship"]),
            weight=float(d.get("weight", 0.5)),
            timestamp=d.get("timestamp", ""),
            investigation_id=d.get("investigation_id", ""),
            evidence=d.get("evidence", {}),
        )


class EvidenceGraph:
    """Directed property graph for one investigation.

    All mutations are thread-safe. Lookups are O(1).
    """

    def __init__(
        self,
        investigation_id: str,
        incident_id: str,
        service: str,
        incident_type: str,
        phase: InvestigationPhase = InvestigationPhase.CREATED,
        created_at: str = "",
    ) -> None:
        self.graph_id         = investigation_id
        self.investigation_id = investigation_id
        self.incident_id      = incident_id
        self.service          = service
        self.incident_type    = incident_type
        self.phase            = phase
        self.schema_version   = SCHEMA_VERSION
        self.created_at       = created_at or datetime.now(timezone.utc).isoformat()

        self._nodes:     dict[str, EvidenceNode] = {}
        self._edges:     dict[str, EvidenceEdge] = {}
        self._out_edges: dict[str, list[str]]    = {}  # src_id → [edge_ids]
        self._in_edges:  dict[str, list[str]]    = {}  # dst_id → [edge_ids]
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_node(self, node: EvidenceNode) -> str:
        """Add or replace a node. Returns node_id."""
        with self._lock:
            self._nodes[node.node_id] = node
            self._out_edges.setdefault(node.node_id, [])
            self._in_edges.setdefault(node.node_id, [])
        return node.node_id

    def add_edge(self, edge: EvidenceEdge) -> str:
        """Add edge. Both src and dst must exist. Returns edge_id."""
        with self._lock:
            if edge.src_id not in self._nodes or edge.dst_id not in self._nodes:
                raise ValueError(
                    f"Edge {edge.edge_id}: src={edge.src_id} or dst={edge.dst_id} not in graph"
                )
            self._edges[edge.edge_id] = edge
            self._out_edges.setdefault(edge.src_id, [])
            if edge.edge_id not in self._out_edges[edge.src_id]:
                self._out_edges[edge.src_id].append(edge.edge_id)
            self._in_edges.setdefault(edge.dst_id, [])
            if edge.edge_id not in self._in_edges[edge.dst_id]:
                self._in_edges[edge.dst_id].append(edge.edge_id)
        return edge.edge_id

    def set_phase(self, phase: InvestigationPhase) -> None:
        self.phase = phase

    # ------------------------------------------------------------------
    # Lookups — O(1)
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> EvidenceNode | None:
        return self._nodes.get(node_id)

    def get_edge(self, edge_id: str) -> EvidenceEdge | None:
        return self._edges.get(edge_id)

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return len(self._edges)

    def all_nodes(self) -> list[EvidenceNode]:
        return list(self._nodes.values())

    def all_edges(self) -> list[EvidenceEdge]:
        return list(self._edges.values())

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    def get_outgoing(
        self, node_id: str, relationship: EdgeRelationship | None = None
    ) -> list[EvidenceNode]:
        """Nodes reachable from node_id via outgoing edges."""
        result = []
        for eid in self._out_edges.get(node_id, []):
            edge = self._edges.get(eid)
            if edge and (relationship is None or edge.relationship == relationship):
                dst = self._nodes.get(edge.dst_id)
                if dst:
                    result.append(dst)
        return result

    def get_incoming(
        self, node_id: str, relationship: EdgeRelationship | None = None
    ) -> list[EvidenceNode]:
        """Nodes that point to node_id via incoming edges."""
        result = []
        for eid in self._in_edges.get(node_id, []):
            edge = self._edges.get(eid)
            if edge and (relationship is None or edge.relationship == relationship):
                src = self._nodes.get(edge.src_id)
                if src:
                    result.append(src)
        return result

    def traverse_from(
        self,
        root_id: str,
        max_hops: int = 3,
        direction: str = "out",  # "out" | "in" | "both"
        relationship: EdgeRelationship | None = None,
    ) -> list[EvidenceNode]:
        """BFS traversal from root. Returns nodes in BFS order (root excluded)."""
        visited: set[str] = {root_id}
        frontier = [root_id]
        result: list[EvidenceNode] = []

        for _ in range(max_hops):
            next_frontier: list[str] = []
            for nid in frontier:
                neighbors: list[EvidenceNode] = []
                if direction in ("out", "both"):
                    neighbors += self.get_outgoing(nid, relationship)
                if direction in ("in", "both"):
                    neighbors += self.get_incoming(nid, relationship)
                for node in neighbors:
                    if node.node_id not in visited:
                        visited.add(node.node_id)
                        result.append(node)
                        next_frontier.append(node.node_id)
            frontier = next_frontier
            if not frontier:
                break

        return result

    def get_timeline(self) -> list[EvidenceNode]:
        """All nodes sorted by timestamp ascending."""
        def _ts(n: EvidenceNode) -> str:
            return n.timestamp or n.collected_at or ""

        return sorted(self._nodes.values(), key=_ts)

    def find_root_cause_candidates(self) -> list[EvidenceNode]:
        """Nodes that are targets of CAUSED_BY edges (i.e., things that caused problems)."""
        candidate_ids: set[str] = set()
        for edge in self._edges.values():
            if edge.relationship == EdgeRelationship.CAUSED_BY:
                candidate_ids.add(edge.dst_id)
        return [self._nodes[nid] for nid in candidate_ids if nid in self._nodes]

    def nodes_by_type(self, node_type: NodeType) -> list[EvidenceNode]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    def nodes_by_source(self, source_type: str) -> list[EvidenceNode]:
        return [n for n in self._nodes.values() if n.source_type == source_type]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id":         self.graph_id,
            "investigation_id": self.investigation_id,
            "incident_id":      self.incident_id,
            "service":          self.service,
            "incident_type":    self.incident_type,
            "phase":            self.phase.value,
            "schema_version":   self.schema_version,
            "created_at":       self.created_at,
            "nodes":            [n.to_dict() for n in self._nodes.values()],
            "edges":            [e.to_dict() for e in self._edges.values()],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvidenceGraph":
        g = cls(
            investigation_id=d["investigation_id"],
            incident_id=d.get("incident_id", ""),
            service=d.get("service", ""),
            incident_type=d.get("incident_type", ""),
            phase=InvestigationPhase(d.get("phase", InvestigationPhase.CREATED.value)),
            created_at=d.get("created_at", ""),
        )
        g.schema_version = d.get("schema_version", SCHEMA_VERSION)
        for nd in d.get("nodes", []):
            g.add_node(EvidenceNode.from_dict(nd))
        for ed in d.get("edges", []):
            try:
                g.add_edge(EvidenceEdge.from_dict(ed))
            except ValueError:
                pass  # skip edges with missing nodes (partial loads)
        return g

    def export_dot(self) -> str:
        """GraphViz DOT export for visualization."""
        lines = [f'digraph "{self.investigation_id}" {{']
        lines.append("  rankdir=LR;")
        for n in self._nodes.values():
            label = f"{n.node_type.value}\\n{n.entity_id}"
            lines.append(f'  "{n.node_id}" [label="{label}"];')
        for e in self._edges.values():
            lines.append(f'  "{e.src_id}" -> "{e.dst_id}" [label="{e.relationship.value}"];')
        lines.append("}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"EvidenceGraph(investigation_id={self.investigation_id!r}, "
            f"nodes={self.node_count()}, edges={self.edge_count()}, "
            f"phase={self.phase.value})"
        )
