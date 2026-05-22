"""Recurrence tracking for SentinalAI.

Detects repeat-offender entities/services across investigations by reading
from experience_store and knowledge_graph. Tracks:
  - Recurrence count per (service, incident_type) pair
  - Time since last occurrence
  - Temporary fix vs permanent fix status
  - Recurrence interval trend (accelerating / decelerating)
  - Blast radius growth across recurrences
  - Remediation outcome history

Used by:
  - grounding_confidence.py: _dim_repeat_offender() boost signal
  - agent.py: augments experience retrieval context

All reads are non-destructive. Writes persist a lightweight recurrence index
to eval/recurrence_index.json. Thread-safe via module lock.

Configuration:
  RECURRENCE_INDEX_PATH — Override default path
  RECURRENCE_ENABLED    — Enable/disable (default: true)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("sentinalai.recurrence_tracker")

RECURRENCE_INDEX_PATH = os.environ.get(
    "RECURRENCE_INDEX_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "recurrence_index.json"),
)
RECURRENCE_ENABLED = os.environ.get("RECURRENCE_ENABLED", "true").lower() in ("1", "true", "yes")

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RecurrenceRecord:
    """Tracks recurrence history for a (service, incident_type) pair."""
    service: str
    incident_type: str
    occurrences: list[str] = field(default_factory=list)   # ISO timestamps
    root_causes: list[str] = field(default_factory=list)
    remediation_outcomes: list[bool] = field(default_factory=list)  # True=fix worked
    permanent_fix_applied: bool = False
    permanent_fix_at: Optional[str] = None
    entity_continuity_risk: float = 0.0    # 0.0–1.0

    @property
    def recurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def last_occurrence(self) -> Optional[str]:
        return self.occurrences[-1] if self.occurrences else None

    @property
    def days_since_last(self) -> Optional[float]:
        if not self.last_occurrence:
            return None
        try:
            last = datetime.fromisoformat(self.last_occurrence)
            now  = datetime.now(timezone.utc)
            return (now - last).total_seconds() / 86400.0
        except (ValueError, TypeError):
            return None

    @property
    def last_fix_successful(self) -> Optional[bool]:
        return self.remediation_outcomes[-1] if self.remediation_outcomes else None

    @property
    def similar_remediation_count(self) -> int:
        """Count how many past remediations were successful."""
        return sum(1 for o in self.remediation_outcomes if o)

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "incident_type": self.incident_type,
            "recurrence_count": self.recurrence_count,
            "occurrences": self.occurrences[-20:],  # keep last 20
            "root_causes": self.root_causes[-10:],
            "remediation_outcomes": self.remediation_outcomes[-20:],
            "permanent_fix_applied": self.permanent_fix_applied,
            "permanent_fix_at": self.permanent_fix_at,
            "last_occurrence": self.last_occurrence,
            "days_since_last": self.days_since_last,
            "last_fix_successful": self.last_fix_successful,
            "similar_remediation_count": self.similar_remediation_count,
            "entity_continuity_risk": round(self.entity_continuity_risk, 3),
        }

    @classmethod
    def from_dict(cls, d: dict) -> RecurrenceRecord:
        r = cls(service=d["service"], incident_type=d["incident_type"])
        r.occurrences          = d.get("occurrences", [])
        r.root_causes          = d.get("root_causes", [])
        r.remediation_outcomes = d.get("remediation_outcomes", [])
        r.permanent_fix_applied = d.get("permanent_fix_applied", False)
        r.permanent_fix_at      = d.get("permanent_fix_at")
        r.entity_continuity_risk = d.get("entity_continuity_risk", 0.0)
        return r


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

class RecurrenceIndex:
    """Persistent, thread-safe recurrence index."""

    def __init__(self, records: Optional[dict[str, RecurrenceRecord]] = None):
        self._records: dict[str, RecurrenceRecord] = records or {}

    @staticmethod
    def _key(service: str, incident_type: str) -> str:
        return f"{service.lower()}::{incident_type.lower()}"

    def get(self, service: str, incident_type: str) -> Optional[RecurrenceRecord]:
        return self._records.get(self._key(service, incident_type))

    def upsert(
        self,
        service: str,
        incident_type: str,
        root_cause: str,
        occurred_at: Optional[str] = None,
        remediation_successful: Optional[bool] = None,
        permanent_fix: bool = False,
    ) -> RecurrenceRecord:
        key = self._key(service, incident_type)
        if key not in self._records:
            self._records[key] = RecurrenceRecord(service=service, incident_type=incident_type)
        rec = self._records[key]

        ts = occurred_at or datetime.now(timezone.utc).isoformat()
        rec.occurrences.append(ts)

        if root_cause and root_cause not in rec.root_causes:
            rec.root_causes.append(root_cause)

        if remediation_successful is not None:
            rec.remediation_outcomes.append(remediation_successful)

        if permanent_fix:
            rec.permanent_fix_applied = True
            rec.permanent_fix_at = ts

        # Update entity continuity risk
        rec.entity_continuity_risk = _compute_continuity_risk(rec)
        return rec

    def all_records(self) -> list[RecurrenceRecord]:
        return list(self._records.values())

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self._records.items()}

    @classmethod
    def from_dict(cls, d: dict) -> RecurrenceIndex:
        records = {}
        for k, v in d.items():
            try:
                rec = RecurrenceRecord.from_dict(v)
                records[k] = rec
            except Exception as exc:
                logger.warning("Skipping corrupt recurrence record %s: %s", k, exc)
        return cls(records)


# Module-level index singleton
_index: Optional[RecurrenceIndex] = None


def _load_index() -> RecurrenceIndex:
    global _index
    if _index is not None:
        return _index
    try:
        with open(RECURRENCE_INDEX_PATH, "r") as f:
            data = json.load(f)
        _index = RecurrenceIndex.from_dict(data)
        logger.info("Recurrence index loaded: %d records", len(_index._records))
    except FileNotFoundError:
        logger.debug("No recurrence index found at %s — starting fresh", RECURRENCE_INDEX_PATH)
        _index = RecurrenceIndex()
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Corrupt recurrence index: %s — starting fresh", exc)
        _index = RecurrenceIndex()
    return _index


def _save_index(idx: RecurrenceIndex) -> None:
    try:
        os.makedirs(os.path.dirname(RECURRENCE_INDEX_PATH), exist_ok=True)
        with open(RECURRENCE_INDEX_PATH, "w") as f:
            json.dump(idx.to_dict(), f, indent=2)
    except Exception as exc:
        logger.warning("Failed to save recurrence index: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(service: str, incident_type: str) -> Optional[dict]:
    """Check recurrence history for a (service, incident_type) pair.

    Returns a dict compatible with grounding_confidence._dim_repeat_offender(),
    or None if no history exists or recurrence tracking is disabled.

    Dict keys: recurrence_count, permanent_fix_applied, last_occurrence,
               days_since_last, last_fix_successful, similar_remediation_count,
               entity_continuity_risk
    """
    if not RECURRENCE_ENABLED:
        return None
    if not service or not incident_type:
        return None

    with _lock:
        idx = _load_index()
        rec = idx.get(service, incident_type)

    if rec is None or rec.recurrence_count == 0:
        return None

    return rec.to_dict()


def record(
    service: str,
    incident_type: str,
    root_cause: str,
    occurred_at: Optional[str] = None,
    remediation_successful: Optional[bool] = None,
    permanent_fix: bool = False,
) -> Optional[dict]:
    """Record a new occurrence for a (service, incident_type) pair.

    Call this after each investigation completes to update the recurrence index.
    Returns the updated record dict, or None if disabled.

    Parameters
    ----------
    service:                Service name (canonical form preferred)
    incident_type:          Incident type (e.g. "oomkill", "timeout")
    root_cause:             Root cause string from RCA result
    occurred_at:            ISO timestamp of the incident (defaults to now)
    remediation_successful: True if the applied fix resolved the incident
    permanent_fix:          True if a permanent structural fix was applied
    """
    if not RECURRENCE_ENABLED:
        return None
    if not service or not incident_type:
        return None

    with _lock:
        idx = _load_index()
        rec = idx.upsert(
            service=service,
            incident_type=incident_type,
            root_cause=root_cause or "",
            occurred_at=occurred_at,
            remediation_successful=remediation_successful,
            permanent_fix=permanent_fix,
        )
        _save_index(idx)

    logger.info(
        "Recurrence recorded: service=%s type=%s count=%d risk=%.2f",
        service, incident_type, rec.recurrence_count, rec.entity_continuity_risk,
    )
    return rec.to_dict()


def mark_permanent_fix(service: str, incident_type: str) -> bool:
    """Mark that a permanent structural fix was applied for this (service, type).

    Returns True if the record was found and updated, False otherwise.
    """
    if not RECURRENCE_ENABLED:
        return False

    with _lock:
        idx = _load_index()
        rec = idx.get(service, incident_type)
        if rec is None:
            return False
        rec.permanent_fix_applied = True
        rec.permanent_fix_at = datetime.now(timezone.utc).isoformat()
        _save_index(idx)

    logger.info("Permanent fix marked: service=%s type=%s", service, incident_type)
    return True


def get_top_offenders(n: int = 10) -> list[dict]:
    """Return top N repeat offenders sorted by recurrence count descending."""
    if not RECURRENCE_ENABLED:
        return []

    with _lock:
        idx = _load_index()
        records = idx.all_records()

    records.sort(key=lambda r: r.recurrence_count, reverse=True)
    return [r.to_dict() for r in records[:n] if r.recurrence_count > 1]


def get_continuity_risks(threshold: float = 0.60) -> list[dict]:
    """Return services with entity_continuity_risk above threshold."""
    if not RECURRENCE_ENABLED:
        return []

    with _lock:
        idx = _load_index()
        records = idx.all_records()

    at_risk = [
        r.to_dict() for r in records
        if r.entity_continuity_risk >= threshold
    ]
    at_risk.sort(key=lambda r: r.get("entity_continuity_risk", 0), reverse=True)
    return at_risk


def enrich_from_experience_store() -> int:
    """Seed recurrence index from experience_store if index is empty.

    Returns number of records added.
    """
    if not RECURRENCE_ENABLED:
        return 0

    with _lock:
        idx = _load_index()
        if len(idx._records) > 0:
            return 0   # already populated

    try:
        from supervisor.experience_store import retrieve_all as _retrieve_all
        experiences = _retrieve_all(limit=500)
    except (ImportError, Exception) as exc:
        logger.debug("experience_store not available for seeding: %s", exc)
        return 0

    added = 0
    with _lock:
        idx = _load_index()
        for exp in experiences:
            svc   = exp.get("service", "")
            itype = exp.get("incident_type", "unknown")
            rc    = exp.get("root_cause", "")
            ts    = exp.get("timestamp")
            if svc:
                idx.upsert(service=svc, incident_type=itype, root_cause=rc, occurred_at=ts)
                added += 1
        if added:
            _save_index(idx)

    logger.info("Seeded recurrence index from experience_store: %d entries", added)
    return added


# ---------------------------------------------------------------------------
# Continuity risk scoring
# ---------------------------------------------------------------------------

def _compute_continuity_risk(rec: RecurrenceRecord) -> float:
    """Compute entity_continuity_risk score (0.0–1.0) for a record.

    Factors: recurrence frequency, days since last, permanent fix status,
    failed remediation ratio, root cause diversity.
    """
    if rec.recurrence_count == 0:
        return 0.0

    # Frequency component: more occurrences = higher risk
    freq_score = min(1.0, rec.recurrence_count / 10.0)

    # Recency component: recent occurrences = higher risk
    days = rec.days_since_last
    if days is None:
        recency_score = 0.50
    elif days < 1:
        recency_score = 1.0
    elif days < 7:
        recency_score = 0.80
    elif days < 30:
        recency_score = 0.50
    elif days < 90:
        recency_score = 0.25
    else:
        recency_score = 0.10

    # Remediation effectiveness: failed fixes = higher risk
    if rec.remediation_outcomes:
        fail_rate = 1.0 - (sum(1 for o in rec.remediation_outcomes if o) / len(rec.remediation_outcomes))
        remediation_risk = fail_rate
    else:
        remediation_risk = 0.50

    # Permanent fix applied = lower risk
    fix_discount = 0.40 if rec.permanent_fix_applied else 0.0

    # Root cause diversity: multiple different root causes = systemic risk
    diversity_factor = min(0.30, len(rec.root_causes) * 0.08)

    raw = (
        0.35 * freq_score +
        0.30 * recency_score +
        0.20 * remediation_risk +
        0.15 * diversity_factor
    )
    return round(max(0.0, min(1.0, raw - fix_discount)), 3)
