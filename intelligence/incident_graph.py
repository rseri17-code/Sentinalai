"""Incident Knowledge Graph — cross-investigation durable typed graph.

Stores named entities (services, hosts, metrics, alerts) and their
typed relationships across incidents. Grows with each investigation.

Unlike EvidenceGraph (per-investigation, file-backed), this graph
persists and accumulates across all investigations in SQLite.

Storage: SQLite ops_intelligence.db (schema migration 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.incident_graph")


def _node_id(node_type: str, label: str, incident_id: str = "") -> str:
    raw = f"{node_type}:{label}:{incident_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _edge_id(source: str, target: str, relationship: str, incident_id: str) -> str:
    raw = f"{source}:{target}:{relationship}:{incident_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class IncidentNode:
    node_id:     str
    incident_id: str
    node_type:   str        # metric|log|event|change|alert|service|host|outcome
    label:       str
    service:     str
    properties:  dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id":     self.node_id,
            "incident_id": self.incident_id,
            "node_type":   self.node_type,
            "label":       self.label,
            "service":     self.service,
            "properties":  self.properties,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "IncidentNode":
        return cls(
            node_id=row["node_id"],
            incident_id=row["incident_id"],
            node_type=row["node_type"],
            label=row["label"],
            service=row["service"] or "",
            properties=json.loads(row["properties"] or "{}"),
            recorded_at=row["recorded_at"],
        )


@dataclass
class IncidentEdge:
    edge_id:        str
    incident_id:    str
    source_node_id: str
    target_node_id: str
    relationship:   str     # CAUSED_BY|PRECEDED|CORRELATED|AFFECTS|etc.
    weight:         float = 1.0
    properties:     dict[str, Any] = field(default_factory=dict)
    recorded_at:    str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id":        self.edge_id,
            "incident_id":    self.incident_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relationship":   self.relationship,
            "weight":         self.weight,
            "properties":     self.properties,
            "recorded_at":    self.recorded_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "IncidentEdge":
        return cls(
            edge_id=row["edge_id"],
            incident_id=row["incident_id"],
            source_node_id=row["source_node_id"],
            target_node_id=row["target_node_id"],
            relationship=row["relationship"],
            weight=float(row["weight"]),
            properties=json.loads(row["properties"] or "{}"),
            recorded_at=row["recorded_at"],
        )


class IncidentGraphStore:
    """SQLite-backed store for cross-investigation incident graph."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def add_node(self, node: IncidentNode) -> None:
        """Insert or ignore (idempotent on node_id + incident_id)."""
        sql = """
            INSERT OR IGNORE INTO incident_graph_nodes
                (node_id, incident_id, node_type, label, service, properties, recorded_at)
            VALUES (?,?,?,?,?,?,?)
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, (
                    node.node_id, node.incident_id, node.node_type,
                    node.label, node.service,
                    json.dumps(node.properties), node.recorded_at,
                ))
        except Exception as exc:
            logger.debug("IncidentGraphStore.add_node failed: %s", exc)

    def add_edge(self, edge: IncidentEdge) -> None:
        """Insert or ignore (idempotent on edge_id)."""
        sql = """
            INSERT OR IGNORE INTO incident_graph_edges
                (edge_id, incident_id, source_node_id, target_node_id,
                 relationship, weight, properties, recorded_at)
            VALUES (?,?,?,?,?,?,?,?)
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, (
                    edge.edge_id, edge.incident_id, edge.source_node_id,
                    edge.target_node_id, edge.relationship, edge.weight,
                    json.dumps(edge.properties), edge.recorded_at,
                ))
        except Exception as exc:
            logger.debug("IncidentGraphStore.add_edge failed: %s", exc)

    def get_incident_nodes(self, incident_id: str) -> list[IncidentNode]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM incident_graph_nodes WHERE incident_id=? ORDER BY recorded_at",
                    (incident_id,),
                ).fetchall()
                return [IncidentNode.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("IncidentGraphStore.get_incident_nodes failed: %s", exc)
            return []

    def get_incident_edges(self, incident_id: str) -> list[IncidentEdge]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM incident_graph_edges WHERE incident_id=? ORDER BY recorded_at",
                    (incident_id,),
                ).fetchall()
                return [IncidentEdge.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("IncidentGraphStore.get_incident_edges failed: %s", exc)
            return []

    def find_related_incidents(
        self,
        service: str,
        node_type: str | None = None,
        limit: int = 20,
    ) -> list[str]:
        """Return distinct incident_ids that share the same service (and optionally node_type)."""
        clauses = ["service=?"]
        params: list[Any] = [service]
        if node_type:
            clauses.append("node_type=?"); params.append(node_type)
        where = "WHERE " + " AND ".join(clauses)
        sql = f"""
            SELECT DISTINCT incident_id FROM incident_graph_nodes
            {where} ORDER BY recorded_at DESC LIMIT ?
        """
        params.append(limit)
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [r["incident_id"] for r in rows]
        except Exception as exc:
            logger.debug("IncidentGraphStore.find_related_incidents failed: %s", exc)
            return []

    def make_node(
        self,
        node_type: str,
        label: str,
        incident_id: str,
        service: str = "",
        properties: dict[str, Any] | None = None,
    ) -> IncidentNode:
        now = datetime.now(timezone.utc).isoformat()
        return IncidentNode(
            node_id=_node_id(node_type, label, incident_id),
            incident_id=incident_id,
            node_type=node_type,
            label=label,
            service=service,
            properties=properties or {},
            recorded_at=now,
        )

    def make_edge(
        self,
        source_node_id: str,
        target_node_id: str,
        relationship: str,
        incident_id: str,
        weight: float = 1.0,
        properties: dict[str, Any] | None = None,
    ) -> IncidentEdge:
        now = datetime.now(timezone.utc).isoformat()
        return IncidentEdge(
            edge_id=_edge_id(source_node_id, target_node_id, relationship, incident_id),
            incident_id=incident_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relationship=relationship,
            weight=weight,
            properties=properties or {},
            recorded_at=now,
        )
