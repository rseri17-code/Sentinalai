"""Alert deduplication — prevent parallel investigations for the same incident.

Fingerprinting:
  hash(affected_service + severity_tier + frozenset(tags)) → 16-char hex

Cooldown windows (configurable):
  critical (sev 1-2): DEDUP_COOLDOWN_CRITICAL_SECS   (default 300 s / 5 min)
  medium   (sev 3):   DEDUP_COOLDOWN_MEDIUM_SECS      (default 900 s / 15 min)
  low      (sev 4-5): DEDUP_COOLDOWN_LOW_SECS          (default 1800 s / 30 min)

Correlation window:
  Alerts within DEDUP_CORRELATION_WINDOW_SECS (default 600 s) of an active
  fingerprint are surfaced as related alerts even if deduplication isn't triggered.

Storage: SQLite WAL file at DEDUP_DB_PATH (default: eval/alert_dedup.db).
Thread-safe: single writer lock; read path lock-free after initial load.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("sentinalai.alert_dedup")

DEDUP_ENABLED = os.environ.get("ALERT_DEDUP_ENABLED", "true").lower() in ("1", "true", "yes")

DEDUP_DB_PATH = os.environ.get(
    "DEDUP_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "alert_dedup.db"),
)

_COOLDOWN: dict[str, int] = {
    "critical": int(os.environ.get("DEDUP_COOLDOWN_CRITICAL_SECS", "300")),
    "medium":   int(os.environ.get("DEDUP_COOLDOWN_MEDIUM_SECS",   "900")),
    "low":      int(os.environ.get("DEDUP_COOLDOWN_LOW_SECS",      "1800")),
}

_CORRELATION_WINDOW = int(os.environ.get("DEDUP_CORRELATION_WINDOW_SECS", "600"))

_lock = threading.Lock()
_instance: Optional["AlertDeduplicator"] = None


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DedupResult:
    is_duplicate: bool
    fingerprint: str
    existing_investigation_id: str = ""
    cooldown_remaining_secs: float = 0.0
    correlated_ids: list[str] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# Core deduplicator
# ---------------------------------------------------------------------------

class AlertDeduplicator:
    """Thread-safe alert deduplication engine."""

    def __init__(self, db_path: str = DEDUP_DB_PATH) -> None:
        self._db_path = db_path
        self._init_db()

    # ── Public API ──────────────────────────────────────────────────────────

    def fingerprint(self, incident_id: str, service: str, severity: int, tags: list[str]) -> str:
        """Produce a stable 16-char hex fingerprint for an alert."""
        severity_tier = _severity_tier(severity)
        canonical_tags = "|".join(sorted(str(t).lower() for t in tags))
        raw = f"{service.lower()}::{severity_tier}::{canonical_tags}"
        return hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()

    def check_and_register(
        self,
        incident_id: str,
        service: str,
        severity: int,
        tags: list[str],
        investigation_id: str,
    ) -> DedupResult:
        """Check for duplicates and register this alert if it's novel.

        If DEDUP_ENABLED is False, always returns is_duplicate=False (passthrough).
        Returns DedupResult with full context for the caller.
        """
        fp = self.fingerprint(incident_id, service, severity, tags)

        if not DEDUP_ENABLED:
            self._register(fp, incident_id, investigation_id, severity)
            return DedupResult(is_duplicate=False, fingerprint=fp)

        cooldown_secs = _COOLDOWN[_severity_tier(severity)]
        now = time.time()

        with _lock:
            row = self._fetch_active(fp, now, cooldown_secs)
            if row:
                remaining = cooldown_secs - (now - row["registered_at"])
                correlated = self._fetch_correlated(fp, now, _CORRELATION_WINDOW)
                logger.info(
                    "Dedup HIT fingerprint=%s incident=%s → existing=%s remaining=%.0fs",
                    fp, incident_id, row["investigation_id"], remaining,
                )
                return DedupResult(
                    is_duplicate=True,
                    fingerprint=fp,
                    existing_investigation_id=row["investigation_id"],
                    cooldown_remaining_secs=round(remaining, 1),
                    correlated_ids=correlated,
                    reason=f"duplicate of {row['investigation_id']} (cooldown {cooldown_secs}s)",
                )

            self._register(fp, incident_id, investigation_id, severity)
            correlated = self._fetch_correlated(fp, now, _CORRELATION_WINDOW)
            return DedupResult(
                is_duplicate=False,
                fingerprint=fp,
                existing_investigation_id=investigation_id,
                correlated_ids=correlated,
            )

    def correlation_window(self, fp: str, window_secs: int = _CORRELATION_WINDOW) -> list[str]:
        """Return investigation IDs correlated with this fingerprint within the window."""
        with _lock:
            return self._fetch_correlated(fp, time.time(), window_secs)

    def expire_old_entries(self, max_age_secs: int = 86400) -> int:
        """Delete entries older than max_age_secs. Returns count deleted."""
        cutoff = time.time() - max_age_secs
        with _lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM dedup_registry WHERE registered_at < ?", (cutoff,)
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def get_stats(self) -> dict:
        """Return runtime statistics."""
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM dedup_registry").fetchone()[0]
            now = time.time()
            active = conn.execute(
                "SELECT COUNT(*) FROM dedup_registry WHERE registered_at > ?",
                (now - max(_COOLDOWN.values()),),
            ).fetchone()[0]
            return {"total_registered": total, "active_within_max_cooldown": active}
        finally:
            conn.close()

    # ── Private helpers ─────────────────────────────────────────────────────

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dedup_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    investigation_id TEXT NOT NULL,
                    severity_tier TEXT NOT NULL DEFAULT 'medium',
                    registered_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_ts ON dedup_registry (fingerprint, registered_at)"
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_active(self, fp: str, now: float, cooldown: int) -> Optional[sqlite3.Row]:
        cutoff = now - cooldown
        conn = self._connect()
        try:
            return conn.execute(
                """SELECT investigation_id, registered_at FROM dedup_registry
                   WHERE fingerprint = ? AND registered_at > ?
                   ORDER BY registered_at DESC LIMIT 1""",
                (fp, cutoff),
            ).fetchone()
        finally:
            conn.close()

    def _fetch_correlated(self, fp: str, now: float, window: int) -> list[str]:
        cutoff = now - window
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT DISTINCT investigation_id FROM dedup_registry
                   WHERE fingerprint = ? AND registered_at > ?
                   ORDER BY registered_at DESC LIMIT 10""",
                (fp, cutoff),
            ).fetchall()
            return [r["investigation_id"] for r in rows]
        finally:
            conn.close()

    def _register(self, fp: str, incident_id: str, investigation_id: str, severity: int) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO dedup_registry
                   (fingerprint, incident_id, investigation_id, severity_tier, registered_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (fp, incident_id, investigation_id, _severity_tier(severity), time.time()),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _severity_tier(severity: int) -> str:
    if severity <= 2:
        return "critical"
    if severity == 3:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

def get_deduplicator() -> AlertDeduplicator:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = AlertDeduplicator()
    return _instance
