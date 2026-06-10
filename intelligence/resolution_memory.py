"""Resolution Outcome Memory — durable, human-validated incident resolutions.

Separates candidate memory (LLM output) from confirmed memory (human-validated).
Only confirmed memories are surfaced to future investigations.

Storage: SQLite ops_intelligence.db via ops_persistence connection.
Never writes unverified LLM guesses as confirmed operational truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.resolution_memory")

_VALIDATION_CANDIDATE = "candidate"
_VALIDATION_CONFIRMED = "confirmed"
_VALIDATION_REJECTED  = "rejected"


def _make_id(*parts: str) -> str:
    raw = ":".join(p for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ResolutionMemory:
    memory_id:           str
    investigation_id:    str
    incident_id:         str
    service:             str
    environment:         str
    incident_type:       str
    symptoms:            list[str]
    detected_root_cause: str
    evidence_used:       list[str]        # source keys used during investigation
    confirmed_resolution: str             # human-confirmed resolution text
    fix_action:          str
    rollback_action:     str
    owner_team:          str
    confidence:          int              # 0-100 from investigation
    validation_status:   str             # candidate | confirmed | rejected
    is_confirmed:        bool
    lesson_learned:      str
    related_incident_ids: list[str]
    mttr_minutes:        float
    recorded_at:         str
    confirmed_at:        str | None = None

    @classmethod
    def from_investigation(
        cls,
        investigation_id: str,
        incident_id: str,
        service: str,
        incident_type: str,
        result: dict[str, Any],
        evidence: dict[str, Any] | None = None,
        environment: str = "",
        owner_team: str = "",
        mttr_minutes: float = 0.0,
    ) -> "ResolutionMemory":
        now = datetime.now(timezone.utc).isoformat()
        symptoms = _extract_symptoms(result, evidence)
        evidence_used = list((evidence or {}).keys())
        remediation = result.get("remediation", {})
        fix_action = (
            remediation.get("immediate_action", "")
            or remediation.get("action", "")
            or result.get("fix_action", "")
        )

        return cls(
            memory_id=_make_id(investigation_id, incident_id, now[:19]),
            investigation_id=investigation_id,
            incident_id=incident_id,
            service=service,
            environment=environment,
            incident_type=incident_type,
            symptoms=symptoms,
            detected_root_cause=result.get("root_cause", ""),
            evidence_used=evidence_used,
            confirmed_resolution="",
            fix_action=fix_action,
            rollback_action=remediation.get("rollback_action", ""),
            owner_team=owner_team,
            confidence=int(result.get("confidence", 0)),
            validation_status=_VALIDATION_CANDIDATE,
            is_confirmed=False,
            lesson_learned="",
            related_incident_ids=[],
            mttr_minutes=mttr_minutes,
            recorded_at=now,
            confirmed_at=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id":           self.memory_id,
            "investigation_id":    self.investigation_id,
            "incident_id":         self.incident_id,
            "service":             self.service,
            "environment":         self.environment,
            "incident_type":       self.incident_type,
            "symptoms":            self.symptoms,
            "detected_root_cause": self.detected_root_cause,
            "evidence_used":       self.evidence_used,
            "confirmed_resolution": self.confirmed_resolution,
            "fix_action":          self.fix_action,
            "rollback_action":     self.rollback_action,
            "owner_team":          self.owner_team,
            "confidence":          self.confidence,
            "validation_status":   self.validation_status,
            "is_confirmed":        self.is_confirmed,
            "lesson_learned":      self.lesson_learned,
            "related_incident_ids": self.related_incident_ids,
            "mttr_minutes":        self.mttr_minutes,
            "recorded_at":         self.recorded_at,
            "confirmed_at":        self.confirmed_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ResolutionMemory":
        return cls(
            memory_id=row["memory_id"],
            investigation_id=row["investigation_id"],
            incident_id=row["incident_id"],
            service=row["service"],
            environment=row["environment"] or "",
            incident_type=row["incident_type"],
            symptoms=json.loads(row["symptoms"] or "[]"),
            detected_root_cause=row["detected_root_cause"],
            evidence_used=json.loads(row["evidence_used"] or "[]"),
            confirmed_resolution=row["confirmed_resolution"] or "",
            fix_action=row["fix_action"] or "",
            rollback_action=row["rollback_action"] or "",
            owner_team=row["owner_team"] or "",
            confidence=int(row["confidence"]),
            validation_status=row["validation_status"],
            is_confirmed=bool(row["is_confirmed"]),
            lesson_learned=row["lesson_learned"] or "",
            related_incident_ids=json.loads(row["related_incident_ids"] or "[]"),
            mttr_minutes=float(row["mttr_minutes"] or 0),
            recorded_at=row["recorded_at"],
            confirmed_at=row["confirmed_at"],
        )


class ResolutionMemoryStore:
    """SQLite-backed store for resolution memories."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def record(self, memory: ResolutionMemory) -> None:
        """Insert or ignore duplicate memory (idempotent on memory_id)."""
        sql = """
            INSERT OR IGNORE INTO resolution_memories (
                memory_id, investigation_id, incident_id, service, environment,
                incident_type, symptoms, detected_root_cause, evidence_used,
                confirmed_resolution, fix_action, rollback_action, owner_team,
                confidence, validation_status, is_confirmed, lesson_learned,
                related_incident_ids, mttr_minutes, recorded_at, confirmed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        params = (
            memory.memory_id, memory.investigation_id, memory.incident_id,
            memory.service, memory.environment, memory.incident_type,
            json.dumps(memory.symptoms), memory.detected_root_cause,
            json.dumps(memory.evidence_used), memory.confirmed_resolution,
            memory.fix_action, memory.rollback_action, memory.owner_team,
            memory.confidence, memory.validation_status, int(memory.is_confirmed),
            memory.lesson_learned, json.dumps(memory.related_incident_ids),
            memory.mttr_minutes, memory.recorded_at, memory.confirmed_at,
        )
        try:
            with self._conn() as conn:
                conn.execute(sql, params)
        except Exception as exc:
            logger.debug("ResolutionMemoryStore.record failed: %s", exc)

    def confirm(
        self,
        memory_id: str,
        confirmed_resolution: str = "",
        lesson_learned: str = "",
        owner_team: str = "",
    ) -> bool:
        """Promote a candidate to confirmed. Returns True if updated."""
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            UPDATE resolution_memories
            SET validation_status=?, is_confirmed=1, confirmed_at=?,
                confirmed_resolution=COALESCE(NULLIF(?,''), confirmed_resolution),
                lesson_learned=COALESCE(NULLIF(?,''), lesson_learned),
                owner_team=COALESCE(NULLIF(?,''), owner_team)
            WHERE memory_id=? AND validation_status='candidate'
        """
        try:
            with self._conn() as conn:
                cur = conn.execute(sql, (
                    _VALIDATION_CONFIRMED, now,
                    confirmed_resolution, lesson_learned, owner_team, memory_id,
                ))
                return cur.rowcount > 0
        except Exception as exc:
            logger.debug("ResolutionMemoryStore.confirm failed: %s", exc)
            return False

    def reject(self, memory_id: str) -> bool:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE resolution_memories SET validation_status=? WHERE memory_id=?",
                    (_VALIDATION_REJECTED, memory_id),
                )
                return cur.rowcount > 0
        except Exception as exc:
            logger.debug("ResolutionMemoryStore.reject failed: %s", exc)
            return False

    def get(self, memory_id: str) -> ResolutionMemory | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM resolution_memories WHERE memory_id=?", (memory_id,)
                ).fetchone()
                return ResolutionMemory.from_row(row) if row else None
        except Exception as exc:
            logger.debug("ResolutionMemoryStore.get failed: %s", exc)
            return None

    def query(
        self,
        service: str | None = None,
        incident_type: str | None = None,
        validation_status: str | None = None,
        confirmed_only: bool = False,
        limit: int = 50,
    ) -> list[ResolutionMemory]:
        clauses: list[str] = []
        params: list[Any] = []
        if service:
            clauses.append("service=?"); params.append(service)
        if incident_type:
            clauses.append("incident_type=?"); params.append(incident_type)
        if confirmed_only:
            clauses.append("is_confirmed=1")
        elif validation_status:
            clauses.append("validation_status=?"); params.append(validation_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM resolution_memories {where} ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [ResolutionMemory.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("ResolutionMemoryStore.query failed: %s", exc)
            return []

    def find_similar(
        self,
        root_cause: str,
        service: str | None = None,
        incident_type: str | None = None,
        confirmed_only: bool = True,
        limit: int = 5,
    ) -> list[ResolutionMemory]:
        """Token-overlap similarity search. Returns best matches."""
        candidates = self.query(
            service=service,
            incident_type=incident_type,
            confirmed_only=confirmed_only,
            limit=500,
        )
        query_tokens = set(root_cause.lower().split())
        scored = []
        for m in candidates:
            target = set(m.detected_root_cause.lower().split())
            if not query_tokens or not target:
                continue
            score = len(query_tokens & target) / len(query_tokens | target)
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:limit]]


def _extract_symptoms(result: dict[str, Any], evidence: dict[str, Any] | None) -> list[str]:
    """Extract deterministic symptom tokens from investigation result and evidence."""
    symptoms: set[str] = set()

    incident_type = result.get("incident_type", "")
    if incident_type:
        symptoms.add(incident_type)

    root_cause = result.get("root_cause", "")
    # Add meaningful tokens (skip stopwords, keep technical terms)
    _STOPWORDS = {"the", "a", "an", "is", "was", "in", "on", "at", "of", "for",
                  "and", "or", "to", "with", "by", "due", "has", "have", "been"}
    for tok in root_cause.lower().split():
        tok = tok.strip(".,;:()[]")
        if len(tok) > 3 and tok not in _STOPWORDS:
            symptoms.add(tok)

    # Pull alert names from evidence if present
    if evidence:
        alerts = evidence.get("alerts", {})
        if isinstance(alerts, dict):
            for k in ("alerts_firing", "firing", "active"):
                for a in (alerts.get(k) or []):
                    if isinstance(a, str):
                        symptoms.add(a.lower())

    return sorted(symptoms)[:20]  # cap at 20
