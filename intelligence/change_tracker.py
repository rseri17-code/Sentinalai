"""Change Impact Intelligence — changes as first-class entities.

Records deployments, config changes, schema changes, and links them to incidents.
Impact scoring is fully deterministic:
  score = time_score * 0.4 + service_score * 0.3 + dep_score * 0.2 + type_score * 0.1

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

logger = logging.getLogger("sentinalai.intelligence.change_tracker")

# Time windows for change-incident correlation
_WINDOW_CRITICAL_HOURS = 1.0   # score=1.0
_WINDOW_LIKELY_HOURS   = 6.0   # score=0.7
_WINDOW_POSSIBLE_HOURS = 24.0  # score=0.4

# Types that commonly cause incidents
_HIGH_RISK_CHANGE_TYPES = frozenset({"deployment", "config", "schema", "infrastructure"})
_LOW_RISK_CHANGE_TYPES  = frozenset({"documentation", "test", "monitoring"})


def _change_id(service: str, change_type: str, deployed_at: str) -> str:
    raw = f"{service}:{change_type}:{deployed_at}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _link_id(change_id: str, incident_id: str) -> str:
    raw = f"{change_id}:{incident_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Change:
    change_id:   str
    service:     str
    change_type: str          # deployment|config|schema|infrastructure|rollback|other
    deployed_at: str          # ISO-8601
    description: str
    deployed_by: str
    metadata:    dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id":   self.change_id,
            "service":     self.service,
            "change_type": self.change_type,
            "deployed_at": self.deployed_at,
            "description": self.description,
            "deployed_by": self.deployed_by,
            "metadata":    self.metadata,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Change":
        return cls(
            change_id=row["change_id"],
            service=row["service"],
            change_type=row["change_type"],
            deployed_at=row["deployed_at"],
            description=row["description"] or "",
            deployed_by=row["deployed_by"] or "",
            metadata=json.loads(row["metadata"] or "{}"),
            recorded_at=row["recorded_at"],
        )


@dataclass
class ChangeImpactLink:
    link_id:          str
    change_id:        str
    incident_id:      str
    investigation_id: str
    impact_score:     float    # 0.0-1.0 deterministic score
    link_reason:      str
    linked_at:        str

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id":          self.link_id,
            "change_id":        self.change_id,
            "incident_id":      self.incident_id,
            "investigation_id": self.investigation_id,
            "impact_score":     round(self.impact_score, 3),
            "link_reason":      self.link_reason,
            "linked_at":        self.linked_at,
        }


def score_change_impact(
    change: Change,
    incident_service: str,
    incident_time_iso: str,
    affected_services: list[str] | None = None,
) -> tuple[float, str]:
    """Deterministic impact score: returns (score 0-1, reason string).

    Components:
      time_score    (0.0-1.0) — proximity to incident window
      service_score (0 or 1)  — exact service match
      dep_score     (0 or 1)  — dependency match (service in affected_services)
      type_score    (0-1)     — change type risk
    """
    # Time score
    try:
        t_change = datetime.fromisoformat(change.deployed_at.replace("Z", "+00:00"))
        t_incident = datetime.fromisoformat(incident_time_iso.replace("Z", "+00:00"))
        delta_h = abs((t_incident - t_change).total_seconds()) / 3600.0
    except (ValueError, TypeError):
        delta_h = float("inf")

    if delta_h <= _WINDOW_CRITICAL_HOURS:
        time_score = 1.0
    elif delta_h <= _WINDOW_LIKELY_HOURS:
        time_score = 0.7
    elif delta_h <= _WINDOW_POSSIBLE_HOURS:
        time_score = 0.4
    else:
        time_score = 0.0

    service_score = 1.0 if change.service == incident_service else 0.0
    dep_score = 1.0 if (affected_services and change.service in affected_services) else 0.0
    if change.change_type in _HIGH_RISK_CHANGE_TYPES:
        type_score = 1.0
    elif change.change_type in _LOW_RISK_CHANGE_TYPES:
        type_score = 0.1
    else:
        type_score = 0.5

    score = (time_score * 0.4 + service_score * 0.3 + dep_score * 0.2 + type_score * 0.1)

    reasons = []
    if time_score >= 0.7:
        reasons.append(f"deployed {delta_h:.1f}h before incident")
    if service_score == 1.0:
        reasons.append("same service")
    if dep_score == 1.0:
        reasons.append("affected dependency")
    if type_score >= 0.9:
        reasons.append(f"high-risk change type ({change.change_type})")

    reason = "; ".join(reasons) if reasons else "low correlation"
    return round(score, 3), reason


class ChangeImpactStore:
    """SQLite-backed store for changes and their incident links."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def record_change(self, change: Change) -> None:
        """Insert or ignore (idempotent on change_id)."""
        sql = """
            INSERT OR IGNORE INTO changes
                (change_id, service, change_type, deployed_at, description,
                 deployed_by, metadata, recorded_at)
            VALUES (?,?,?,?,?,?,?,?)
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, (
                    change.change_id, change.service, change.change_type,
                    change.deployed_at, change.description, change.deployed_by,
                    json.dumps(change.metadata), change.recorded_at,
                ))
        except Exception as exc:
            logger.debug("ChangeImpactStore.record_change failed: %s", exc)

    def link_to_incident(self, link: ChangeImpactLink) -> None:
        """Record a change→incident impact link (idempotent on link_id)."""
        sql = """
            INSERT OR IGNORE INTO change_incident_links
                (link_id, change_id, incident_id, investigation_id,
                 impact_score, link_reason, linked_at)
            VALUES (?,?,?,?,?,?,?)
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, (
                    link.link_id, link.change_id, link.incident_id,
                    link.investigation_id, link.impact_score,
                    link.link_reason, link.linked_at,
                ))
        except Exception as exc:
            logger.debug("ChangeImpactStore.link_to_incident failed: %s", exc)

    def get_changes_for_investigation(
        self,
        investigation_id: str,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Return scored changes linked to an investigation."""
        sql = """
            SELECT c.*, l.impact_score, l.link_reason, l.investigation_id
            FROM change_incident_links l
            JOIN changes c ON c.change_id = l.change_id
            WHERE l.investigation_id=? AND l.impact_score >= ?
            ORDER BY l.impact_score DESC
        """
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, (investigation_id, min_score)).fetchall()
                result = []
                for r in rows:
                    d = Change.from_row(r).to_dict()
                    d["impact_score"] = round(float(r["impact_score"]), 3)
                    d["link_reason"] = r["link_reason"]
                    result.append(d)
                return result
        except Exception as exc:
            logger.debug("ChangeImpactStore.get_changes_for_investigation failed: %s", exc)
            return []

    def recent_changes(
        self,
        service: str | None = None,
        hours: float = 24.0,
        limit: int = 50,
    ) -> list[Change]:
        """Return recent changes, optionally filtered by service."""
        cutoff_unix = __import__("time").time() - hours * 3600
        clauses = ["unixepoch(deployed_at) >= ?"]
        params: list[Any] = [cutoff_unix]
        if service:
            clauses.append("service=?"); params.append(service)
        where = "WHERE " + " AND ".join(clauses)
        sql = f"SELECT * FROM changes {where} ORDER BY deployed_at DESC LIMIT ?"
        params.append(limit)
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [Change.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("ChangeImpactStore.recent_changes failed: %s", exc)
            return []

    def make_change(
        self,
        service: str,
        change_type: str,
        deployed_at: str,
        description: str = "",
        deployed_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Change:
        now = datetime.now(timezone.utc).isoformat()
        return Change(
            change_id=_change_id(service, change_type, deployed_at),
            service=service,
            change_type=change_type,
            deployed_at=deployed_at,
            description=description,
            deployed_by=deployed_by,
            metadata=metadata or {},
            recorded_at=now,
        )

    def make_link(
        self,
        change_id: str,
        incident_id: str,
        investigation_id: str,
        impact_score: float,
        link_reason: str,
    ) -> ChangeImpactLink:
        now = datetime.now(timezone.utc).isoformat()
        return ChangeImpactLink(
            link_id=_link_id(change_id, incident_id),
            change_id=change_id,
            incident_id=incident_id,
            investigation_id=investigation_id,
            impact_score=impact_score,
            link_reason=link_reason,
            linked_at=now,
        )
