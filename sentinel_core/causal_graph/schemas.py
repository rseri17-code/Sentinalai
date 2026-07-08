"""Container schemas for Cross-Incident Causal Graph."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from sentinel_core.causal_graph.causal_edge import CausalEdge
from sentinel_core.causal_graph.causal_node import CausalNode


CAUSAL_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Immutable graph container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CausalGraph:
    nodes: tuple[CausalNode, ...] = ()
    edges: tuple[CausalEdge, ...] = ()
    schema_version: int = CAUSAL_SCHEMA_VERSION

    def node_count(self) -> int:
        return len(self.nodes)

    def edge_count(self) -> int:
        return len(self.edges)

    def nodes_by_type(self, node_type: str) -> tuple[CausalNode, ...]:
        return tuple(n for n in self.nodes if n.node_type == node_type)

    def edges_by_type(self, edge_type: str) -> tuple[CausalEdge, ...]:
        return tuple(e for e in self.edges if e.edge_type == edge_type)

    def find_node(self, node_id: str) -> CausalNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "node_count":     self.node_count(),
            "edge_count":     self.edge_count(),
            "nodes":          [asdict(n) for n in
                                 sorted(self.nodes, key=lambda x: x.node_id)],
            "edges":          [asdict(e) for e in
                                 sorted(self.edges, key=lambda x: x.edge_id)],
        }


# ---------------------------------------------------------------------------
# Chains + paths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CausalChain:
    chain_id:       str
    node_ids:       tuple[str, ...]
    count:          int
    confidence:     float = 0.0
    average_mtti_ms: int = 0
    memory_ids:     tuple[str, ...] = ()
    schema_version: int = CAUSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":  self.schema_version,
            "chain_id":        self.chain_id,
            "node_ids":        list(self.node_ids),
            "count":           int(self.count),
            "confidence":      round(float(self.confidence), 4),
            "average_mtti_ms": int(self.average_mtti_ms),
            "memory_ids":      sorted(self.memory_ids),
        }


@dataclass(frozen=True)
class CausalPath:
    """A directed sequence of node ids with an aggregate weight."""
    path_id:      str
    node_ids:     tuple[str, ...]
    weight:       float = 0.0
    schema_version: int = CAUSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "path_id":        self.path_id,
            "node_ids":       list(self.node_ids),
            "weight":         round(float(self.weight), 4),
        }


@dataclass(frozen=True)
class CausalRecurrence:
    signature:      str
    count:          int
    memory_ids:     tuple[str, ...] = ()
    kind:           str = ""             # "root_cause" | "symptom" | ...
    average_confidence: int = 0
    average_mtti_ms:    int = 0
    schema_version: int = CAUSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":     self.schema_version,
            "signature":          self.signature,
            "kind":               self.kind,
            "count":              int(self.count),
            "memory_ids":         sorted(self.memory_ids),
            "average_confidence": int(self.average_confidence),
            "average_mtti_ms":    int(self.average_mtti_ms),
        }


@dataclass(frozen=True)
class RCAPath:
    path_id:        str
    service:        str
    symptom:        str
    root_cause:     str
    evidence_keys:  tuple[str, ...]
    confidence:     float = 0.0
    recurrence:     int   = 1
    memory_ids:     tuple[str, ...] = ()
    schema_version: int   = CAUSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "path_id":        self.path_id,
            "service":        self.service,
            "symptom":        self.symptom,
            "root_cause":     self.root_cause,
            "evidence_keys":  sorted(self.evidence_keys),
            "confidence":     round(float(self.confidence), 4),
            "recurrence":     int(self.recurrence),
            "memory_ids":     sorted(self.memory_ids),
        }


@dataclass(frozen=True)
class MTTIPath:
    path_id:            str
    service:            str
    root_cause:         str
    evidence_ordering:  tuple[str, ...]
    remediation:        str = ""
    average_mtti_ms:    int = 0
    best_mtti_ms:       int = 0
    memory_ids:         tuple[str, ...] = ()
    schema_version:     int = CAUSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":    self.schema_version,
            "path_id":           self.path_id,
            "service":           self.service,
            "root_cause":        self.root_cause,
            "evidence_ordering": list(self.evidence_ordering),
            "remediation":       self.remediation,
            "average_mtti_ms":   int(self.average_mtti_ms),
            "best_mtti_ms":      int(self.best_mtti_ms),
            "memory_ids":        sorted(self.memory_ids),
        }


@dataclass(frozen=True)
class CausalRecommendation:
    kind:               str
    message:            str
    evidence:           tuple[str, ...] = ()
    priority:           int = 100
    related_services:   tuple[str, ...] = ()
    related_root_causes: tuple[str, ...] = ()
    schema_version:     int = CAUSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":       self.schema_version,
            "kind":                 self.kind,
            "message":              self.message,
            "priority":             int(self.priority),
            "evidence":             list(self.evidence),
            "related_services":     sorted(self.related_services),
            "related_root_causes":  sorted(self.related_root_causes),
        }


# Deterministic id helpers -------------------------------------------------

def make_chain_id(node_ids: tuple[str, ...]) -> str:
    """Deterministic 16-hex chain id.

    RC-G: previously joined node_ids with ``","`` — an id containing
    a literal comma collided with the two-element tuple. Framed JSON
    serialisation escapes such characters and closes the collision.
    """
    raw = json.dumps(list(node_ids), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def make_path_id(*parts: str) -> str:
    """Deterministic 16-hex path id.

    RC-G: same framed-JSON discipline as :func:`make_chain_id`.
    """
    raw = json.dumps([str(p) for p in parts], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


__all__ = [
    "CAUSAL_SCHEMA_VERSION",
    "CausalGraph",
    "CausalChain",
    "CausalPath",
    "CausalRecurrence",
    "RCAPath",
    "MTTIPath",
    "CausalRecommendation",
    "make_chain_id",
    "make_path_id",
]
