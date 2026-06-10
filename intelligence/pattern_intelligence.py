"""Pattern Intelligence — deterministic recurring failure pattern detection.

Groups incidents by symptom signature (sha256 of sorted canonical tokens).
No LLM involvement: pure token extraction + Jaccard similarity grouping.

Storage: SQLite ops_intelligence.db via ops_persistence connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.pattern_intelligence")

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "was", "in", "on", "at", "of", "for",
    "and", "or", "to", "with", "by", "due", "has", "have", "been",
    "that", "this", "from", "which", "when", "where", "what",
})


def _canonical_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for tok in text.lower().split():
        tok = tok.strip(".,;:()[]!?\"'`<>")
        if len(tok) > 3 and tok not in _STOPWORDS:
            tokens.append(tok)
    return sorted(set(tokens))[:30]


def _symptom_signature(tokens: list[str]) -> str:
    raw = ":".join(tokens)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _make_pattern_id(*parts: str) -> str:
    raw = ":".join(p for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class OperationalPattern:
    pattern_id:         str   # sha256[:16] of symptom signature
    symptom_signature:  str   # canonical symptom string (":"-joined tokens)
    incident_type:      str
    services:           list[str]
    canonical_symptoms: list[str]
    occurrence_count:   int
    success_count:      int
    first_seen:         str
    last_seen:          str

    @property
    def success_rate(self) -> float:
        return self.success_count / self.occurrence_count if self.occurrence_count > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id":         self.pattern_id,
            "symptom_signature":  self.symptom_signature,
            "incident_type":      self.incident_type,
            "services":           self.services,
            "canonical_symptoms": self.canonical_symptoms,
            "occurrence_count":   self.occurrence_count,
            "success_count":      self.success_count,
            "success_rate":       round(self.success_rate, 3),
            "first_seen":         self.first_seen,
            "last_seen":          self.last_seen,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OperationalPattern":
        return cls(
            pattern_id=row["pattern_id"],
            symptom_signature=row["symptom_signature"],
            incident_type=row["incident_type"],
            services=json.loads(row["services"] or "[]"),
            canonical_symptoms=json.loads(row["canonical_symptoms"] or "[]"),
            occurrence_count=int(row["occurrence_count"]),
            success_count=int(row["success_count"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )


class PatternIntelligenceStore:
    """SQLite-backed store for operational patterns."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def record_occurrence(
        self,
        incident_type: str,
        root_cause: str,
        service: str,
        resolved: bool,
    ) -> str:
        """Extract pattern from root cause, upsert into store. Returns pattern_id."""
        tokens = _canonical_tokens(root_cause)
        if not tokens:
            return ""
        sig = ":".join(tokens)
        pattern_id = _symptom_signature(tokens)
        now = datetime.now(timezone.utc).isoformat()

        sql_upsert = """
            INSERT INTO operational_patterns
                (pattern_id, symptom_signature, incident_type, services,
                 canonical_symptoms, occurrence_count, success_count,
                 first_seen, last_seen)
            VALUES (?,?,?,?,?,1,?,?,?)
            ON CONFLICT(pattern_id) DO UPDATE SET
                occurrence_count = occurrence_count + 1,
                success_count    = success_count + excluded.success_count,
                last_seen        = excluded.last_seen,
                services         = excluded.services
        """
        services_json = json.dumps(sorted({service}) if service else [])
        try:
            with self._conn() as conn:
                conn.execute(sql_upsert, (
                    pattern_id, sig, incident_type, services_json,
                    json.dumps(tokens), 1 if resolved else 0, now, now,
                ))
        except Exception as exc:
            logger.debug("PatternIntelligenceStore.record_occurrence failed: %s", exc)
        return pattern_id

    def get(self, pattern_id: str) -> OperationalPattern | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM operational_patterns WHERE pattern_id=?",
                    (pattern_id,),
                ).fetchone()
                return OperationalPattern.from_row(row) if row else None
        except Exception as exc:
            logger.debug("PatternIntelligenceStore.get failed: %s", exc)
            return None

    def query(
        self,
        incident_type: str | None = None,
        service: str | None = None,
        min_occurrences: int = 1,
        limit: int = 50,
    ) -> list[OperationalPattern]:
        clauses: list[str] = ["occurrence_count >= ?"]
        params: list[Any] = [min_occurrences]
        if incident_type:
            clauses.append("incident_type=?"); params.append(incident_type)
        if service:
            clauses.append("services LIKE ?"); params.append(f'%"{service}"%')
        where = "WHERE " + " AND ".join(clauses)
        sql = f"SELECT * FROM operational_patterns {where} ORDER BY occurrence_count DESC LIMIT ?"
        params.append(limit)
        try:
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [OperationalPattern.from_row(r) for r in rows]
        except Exception as exc:
            logger.debug("PatternIntelligenceStore.query failed: %s", exc)
            return []

    def find_similar(
        self,
        root_cause: str,
        min_jaccard: float = 0.40,
        limit: int = 5,
    ) -> list[OperationalPattern]:
        """Return patterns whose symptom tokens overlap with root_cause tokens."""
        query_tokens = set(_canonical_tokens(root_cause))
        if not query_tokens:
            return []
        candidates = self.query(limit=500)
        scored: list[tuple[float, OperationalPattern]] = []
        for p in candidates:
            target = set(p.canonical_symptoms)
            if not target:
                continue
            score = len(query_tokens & target) / len(query_tokens | target)
            if score >= min_jaccard:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored[:limit]]
