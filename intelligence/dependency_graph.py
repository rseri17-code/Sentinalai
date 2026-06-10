"""Service Dependency Graph — persisted living service topology.

Inferred from evidence: when service A's investigation reveals B as upstream,
that dependency is recorded and strength-weighted over time.

Storage: SQLite ops_intelligence.db (schema migration 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.dependency_graph")


def _dep_id(source: str, target: str, dep_type: str) -> str:
    raw = f"{source}:{target}:{dep_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ServiceDependency:
    dep_id:         str
    source_service: str   # downstream (depends-on target)
    target_service: str   # upstream (is depended upon)
    dep_type:       str   # runtime|async|database|cache|queue|storage
    strength:       float # 0.0-1.0; increases with each corroborating observation
    observed_count: int
    first_seen:     str
    last_seen:      str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dep_id":         self.dep_id,
            "source_service": self.source_service,
            "target_service": self.target_service,
            "dep_type":       self.dep_type,
            "strength":       round(self.strength, 3),
            "observed_count": self.observed_count,
            "first_seen":     self.first_seen,
            "last_seen":      self.last_seen,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ServiceDependency":
        return cls(
            dep_id=row["dep_id"],
            source_service=row["source_service"],
            target_service=row["target_service"],
            dep_type=row["dep_type"],
            strength=float(row["strength"]),
            observed_count=int(row["observed_count"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )


class DependencyGraphStore:
    """SQLite-backed store for service dependency topology."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def record_dependency(
        self,
        source_service: str,
        target_service: str,
        dep_type: str = "runtime",
        strength_delta: float = 0.1,
    ) -> str:
        """Upsert a dependency edge, strengthening it on repeated observation."""
        dep_id = _dep_id(source_service, target_service, dep_type)
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            INSERT INTO service_dependencies
                (dep_id, source_service, target_service, dep_type,
                 strength, observed_count, first_seen, last_seen)
            VALUES (?,?,?,?,?,1,?,?)
            ON CONFLICT(dep_id) DO UPDATE SET
                observed_count = observed_count + 1,
                strength       = MIN(1.0, strength + ?),
                last_seen      = excluded.last_seen
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, (
                    dep_id, source_service, target_service, dep_type,
                    min(1.0, strength_delta), now, now,
                    strength_delta,
                ))
        except Exception as exc:
            logger.debug("DependencyGraphStore.record_dependency failed: %s", exc)
        return dep_id

    def get_upstream(self, service: str) -> list[ServiceDependency]:
        """Return services that `service` depends on (target_service where source=service)."""
        return self._query_deps("source_service", service)

    def get_downstream(self, service: str) -> list[ServiceDependency]:
        """Return services that depend on `service` (source_service where target=service)."""
        return self._query_deps("target_service", service)

    def get_affected_services(self, failing_service: str) -> list[str]:
        """Return names of services that depend on `failing_service`, sorted by strength desc."""
        deps = self.get_downstream(failing_service)
        deps.sort(key=lambda d: -d.strength)
        return [d.source_service for d in deps]

    def _query_deps(self, column: str, value: str) -> list[ServiceDependency]:
        sql = f"SELECT * FROM service_dependencies WHERE {column}=? ORDER BY strength DESC"
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, (value,)).fetchall()
                return [ServiceDependency.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("DependencyGraphStore._query_deps failed: %s", exc)
            return []

    def all_dependencies(self, limit: int = 200) -> list[ServiceDependency]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM service_dependencies ORDER BY strength DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [ServiceDependency.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("DependencyGraphStore.all_dependencies failed: %s", exc)
            return []
