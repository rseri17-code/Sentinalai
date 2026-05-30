"""Co-failure index — Phase 2 of the SRE Agent Harness learning loop.

Tracks which pairs of services have historically failed together
within a short time window, and how quickly failure propagates
between them.  This answers the question the current system cannot:

    "When service A fails, which services fail within N minutes, and
     in what percentage of A's incidents?"

The index is updated after every investigation via _persist_results.
During RCA it enriches the blast-radius estimate and the hypothesis
ranking (co-failure partners are included in the cascading hypothesis).

Persists to eval/co_failure_index.json; writes are atomic via tmp-swap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("sentinalai.co_failure_index")

_DEFAULT_STORE = os.getenv("CO_FAILURE_INDEX_PATH", "eval/co_failure_index.json")
_PROPAGATION_WINDOW_SECONDS = 300   # 5-min window counts as co-failure


@dataclass
class CoFailureStats:
    """Statistics for a (service_a, service_b) co-failure pair.

    Attributes:
        service_a:              The service that failed first (trigger).
        service_b:              The service that co-failed.
        co_failure_count:       Number of times both failed in the same incident.
        total_incidents_a:      Total incidents for service_a (denominator).
        avg_delay_seconds:      Average seconds from A failure to B failure.
        min_delay_seconds:      Fastest observed propagation.
        max_delay_seconds:      Slowest observed propagation still within window.
    """

    service_a: str
    service_b: str
    co_failure_count: int = 0
    total_incidents_a: int = 0
    avg_delay_seconds: float = 0.0
    min_delay_seconds: float = 0.0
    max_delay_seconds: float = 0.0

    @property
    def co_failure_rate(self) -> float:
        """Fraction of service_a incidents where service_b also failed."""
        if self.total_incidents_a == 0:
            return 0.0
        return self.co_failure_count / self.total_incidents_a

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CoFailureStats":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class CoFailureIndex:
    """Thread-safe index of service co-failure patterns."""

    def __init__(self, store_path: str = _DEFAULT_STORE):
        self._path = store_path
        self._lock = threading.Lock()
        # keyed by "service_a|service_b" (alphabetical within pair)
        self._stats: dict[str, CoFailureStats] = {}
        # per-service incident count (for co_failure_rate denominator)
        self._incident_counts: dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_investigation(
        self,
        primary_service: str,
        co_failing_services: list[str],
        delay_seconds: float = 0.0,
    ) -> None:
        """Record an incident where *primary_service* triggered co-failures.

        Args:
            primary_service:      The service at the root of the incident.
            co_failing_services:  Other services that failed in the same incident.
            delay_seconds:        Average observed delay from primary to co-failure.
                                  Pass 0 if unknown; used only for averaging.
        """
        if not primary_service:
            return

        with self._lock:
            self._incident_counts[primary_service] = (
                self._incident_counts.get(primary_service, 0) + 1
            )
            total_a = self._incident_counts[primary_service]

            # Update total_incidents_a on ALL existing stats for primary_service
            # so the denominator stays correct even on incidents with no co-failures.
            for stat in self._stats.values():
                if stat.service_a == primary_service or stat.service_b == primary_service:
                    # Whichever side is primary_service drives the denominator
                    stat.total_incidents_a = total_a

            for svc in co_failing_services:
                if not svc or svc == primary_service:
                    continue
                key = _pair_key(primary_service, svc)
                a, b = _pair_sorted(primary_service, svc)
                if key not in self._stats:
                    self._stats[key] = CoFailureStats(
                        service_a=a,
                        service_b=b,
                        total_incidents_a=total_a,
                    )
                stat = self._stats[key]
                stat.co_failure_count += 1
                # Rolling average for delay
                n = stat.co_failure_count
                old_avg = stat.avg_delay_seconds
                stat.avg_delay_seconds = (old_avg * (n - 1) + delay_seconds) / n
                if stat.min_delay_seconds == 0.0 or delay_seconds < stat.min_delay_seconds:
                    stat.min_delay_seconds = delay_seconds
                if delay_seconds > stat.max_delay_seconds:
                    stat.max_delay_seconds = delay_seconds

            self._save()

    def get_co_failures(
        self,
        service: str,
        min_rate: float = 0.20,
        top_k: int = 5,
    ) -> list[CoFailureStats]:
        """Return services that co-fail with *service* above *min_rate*.

        Results are sorted by co_failure_rate descending.
        """
        with self._lock:
            results = []
            for stat in self._stats.values():
                if stat.service_a != service and stat.service_b != service:
                    continue
                if stat.co_failure_rate < min_rate:
                    continue
                results.append(stat)
            results.sort(key=lambda s: s.co_failure_rate, reverse=True)
            return results[:top_k]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._stats = {
                _pair_key(r["service_a"], r["service_b"]): CoFailureStats.from_dict(r)
                for r in raw.get("stats", [])
                if isinstance(r, dict) and "service_a" in r and "service_b" in r
            }
            self._incident_counts = raw.get("incident_counts", {})
            logger.info("CoFailureIndex loaded %d pairs from %s", len(self._stats), self._path)
        except FileNotFoundError:
            self._stats = {}
            self._incident_counts = {}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("CoFailureIndex load failed: %s — starting empty", exc)
            self._stats = {}
            self._incident_counts = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        tmp = self._path + ".tmp"
        try:
            payload = {
                "stats": [s.to_dict() for s in self._stats.values()],
                "incident_counts": self._incident_counts,
            }
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("CoFailureIndex save failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def _pair_sorted(a: str, b: str) -> tuple[str, str]:
    s = sorted([a, b])
    return s[0], s[1]


# Module-level singleton
_index: CoFailureIndex | None = None
_index_lock = threading.Lock()


def get_co_failure_index(store_path: str = _DEFAULT_STORE) -> CoFailureIndex:
    global _index
    with _index_lock:
        if _index is None:
            _index = CoFailureIndex(store_path)
        return _index
