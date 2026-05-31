"""Blast Radius History — predicted vs. actual affected services per incident.

After each investigation, records what the blast radius analysis predicted
would be affected and what services actually co-failed (from evidence).
The Jaccard accuracy between prediction and reality feeds back into the
Dependency Behavior layer of Operational Memory.

Answers: "How accurate are our blast radius predictions for service X?"
         "Are we systematically over- or under-predicting cascades?"

Persists to eval/blast_radius_history.json; writes are atomic via tmp-swap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.blast_radius_history")

_DEFAULT_STORE = os.getenv("BLAST_RADIUS_HISTORY_PATH", "eval/blast_radius_history.json")
_MAX_RECORDS = 1000


@dataclass
class BlastRadiusRecord:
    """One incident's predicted vs. actual blast radius outcome.

    Attributes:
        incident_id:        Incident this record belongs to.
        target_service:     Service the fix was applied to (root of blast).
        predicted_services: Services the blast radius model predicted would fail.
        actual_services:    Services that actually showed failure evidence.
        accuracy:           Jaccard similarity of predicted vs actual (0–1).
        timestamp:          ISO-8601 record creation time.
    """

    incident_id: str
    target_service: str
    predicted_services: list[str] = field(default_factory=list)
    actual_services: list[str] = field(default_factory=list)
    accuracy: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BlastRadiusRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 1.0  # both empty → perfect match
    return len(sa & sb) / len(union)


class BlastRadiusHistory:
    """Thread-safe store of blast radius prediction accuracy records."""

    def __init__(self, store_path: str = _DEFAULT_STORE) -> None:
        self._path = store_path
        self._lock = threading.Lock()
        self._records: list[BlastRadiusRecord] = []
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        incident_id: str,
        target_service: str,
        predicted_services: list[str],
        actual_services: list[str],
    ) -> BlastRadiusRecord:
        """Record a blast radius outcome and compute Jaccard accuracy.

        Args:
            incident_id:        Incident identifier.
            target_service:     Root service of the fix.
            predicted_services: Names predicted by compute_blast_radius.
            actual_services:    Names that actually co-failed (from evidence).

        Returns:
            The stored BlastRadiusRecord.
        """
        acc = _jaccard(predicted_services, actual_services)
        rec = BlastRadiusRecord(
            incident_id=incident_id,
            target_service=target_service,
            predicted_services=sorted(set(predicted_services)),
            actual_services=sorted(set(actual_services)),
            accuracy=round(acc, 4),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._records.append(rec)
            if len(self._records) > _MAX_RECORDS:
                self._records = self._records[-_MAX_RECORDS:]
            self._save()
        logger.debug(
            "BlastRadiusHistory: %s predicted=%d actual=%d accuracy=%.2f",
            incident_id, len(predicted_services), len(actual_services), acc,
        )
        return rec

    def get_accuracy_for_service(self, service: str, last_n: int = 20) -> float:
        """Return average Jaccard accuracy for *service* over the last *last_n* records."""
        with self._lock:
            relevant = [r for r in self._records if r.target_service == service]
        if not relevant:
            return 0.0
        recent = relevant[-last_n:]
        return sum(r.accuracy for r in recent) / len(recent)

    def get_history(self, limit: int = 50) -> list[BlastRadiusRecord]:
        """Return the most recent *limit* records, newest first."""
        with self._lock:
            return list(reversed(self._records[-limit:]))

    def get_summary(self) -> dict[str, Any]:
        """Return per-service accuracy summary."""
        with self._lock:
            records = list(self._records)
        services: dict[str, list[float]] = {}
        for r in records:
            services.setdefault(r.target_service, []).append(r.accuracy)
        return {
            svc: {
                "count": len(accs),
                "avg_accuracy": round(sum(accs) / len(accs), 4),
                "min_accuracy": round(min(accs), 4),
            }
            for svc, accs in services.items()
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._records = [
                BlastRadiusRecord.from_dict(r)
                for r in raw
                if isinstance(r, dict) and "incident_id" in r
            ]
            logger.info(
                "BlastRadiusHistory loaded %d records from %s",
                len(self._records), self._path,
            )
        except FileNotFoundError:
            self._records = []
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("BlastRadiusHistory load failed: %s — starting empty", exc)
            self._records = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump([r.to_dict() for r in self._records], f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("BlastRadiusHistory save failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_history: BlastRadiusHistory | None = None
_history_lock = threading.Lock()


def get_blast_radius_history(store_path: str = _DEFAULT_STORE) -> BlastRadiusHistory:
    global _history
    with _history_lock:
        if _history is None:
            _history = BlastRadiusHistory(store_path)
        return _history
