"""Pattern Registry — Phase 2 of the SRE Agent Harness learning loop.

Maintains a library of failure signatures extracted from incident DNA vectors.
Each PatternRecord represents a recurring failure profile that the agent has
seen before, indexed by a stable content-addressed fingerprint.

The registry is consulted during RCA hypothesis generation:
  PatternRegistry.match(dna, incident_type) → ranked PatternRecord list

After each investigation it is updated:
  PatternRegistry.record(investigation_data)          # add/update
  PatternRegistry.update_outcome(fingerprint, ...)    # promote/demote

Persists to eval/pattern_registry.json; writes are atomic via tmp-swap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("sentinalai.pattern_registry")

_DEFAULT_STORE = os.getenv("PATTERN_REGISTRY_PATH", "eval/pattern_registry.json")
_MIN_MATCH_SIMILARITY = 0.72   # cosine threshold to consider a match
_MAX_RECORDS = 500             # cap to keep the registry bounded


@dataclass
class PatternRecord:
    """A recurring failure pattern indexed by DNA fingerprint.

    Attributes:
        fingerprint:        Stable 16-hex content-addressed ID.
        incident_type:      Dominant incident type for this pattern.
        signal_sequence:    Ordered list of signal names that appeared
                            in investigations matching this pattern
                            (most frequent ordering preserved).
        recommended_steps:  Worker actions that have been effective for
                            this pattern (ordered by success rate desc).
        match_count:        How many investigations share this fingerprint.
        hypothesis_outcomes: {hypothesis_name: {"correct": int, "incorrect": int}}
        last_seen:          ISO8601 timestamp of most recent match.
        features:           Representative 16-dim feature vector (centroid
                            of all matched incidents).
    """

    fingerprint: str
    incident_type: str
    signal_sequence: list[str] = field(default_factory=list)
    recommended_steps: list[str] = field(default_factory=list)
    match_count: int = 0
    hypothesis_outcomes: dict[str, dict[str, int]] = field(default_factory=dict)
    last_seen: str = ""
    features: list[float] = field(default_factory=list)

    # Derived read-only helpers ------------------------------------------------

    def top_hypothesis(self) -> str | None:
        """Hypothesis with the highest correct/(correct+incorrect) rate (≥2 samples)."""
        best, best_rate = None, 0.0
        for name, counts in self.hypothesis_outcomes.items():
            total = counts.get("correct", 0) + counts.get("incorrect", 0)
            if total < 2:
                continue
            rate = counts.get("correct", 0) / total
            if rate > best_rate:
                best, best_rate = name, rate
        return best

    def success_rate(self, hypothesis_name: str) -> float:
        counts = self.hypothesis_outcomes.get(hypothesis_name, {})
        total = counts.get("correct", 0) + counts.get("incorrect", 0)
        return counts.get("correct", 0) / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PatternRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class PatternRegistry:
    """Thread-safe pattern library with DNA-based fingerprint lookup."""

    def __init__(self, store_path: str = _DEFAULT_STORE):
        self._path = store_path
        self._lock = threading.Lock()
        self._records: dict[str, PatternRecord] = {}  # fingerprint → record
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(
        self,
        dna_features: list[float],
        incident_type: str,
        top_k: int = 3,
    ) -> list[PatternRecord]:
        """Return the top-k patterns whose feature vector is closest to *dna_features*.

        Uses cosine similarity; only returns records above _MIN_MATCH_SIMILARITY.
        Same-type records are ranked first; cross-type matches are included but
        ranked lower (cosine score * 0.85 penalty).
        """
        from supervisor.incident_dna import _cosine_similarity  # local import avoids circular

        # R1: during an investigation, match against the pinned Frozen Corpus
        # snapshot rather than the live (mutating) singleton records.
        from supervisor.frozen_corpus import _frozen_or_live
        _frozen = _frozen_or_live("pattern_registry")
        _records = self._records if _frozen is None else {
            r["fingerprint"]: PatternRecord.from_dict(r) for r in _frozen
            if isinstance(r, dict) and "fingerprint" in r}

        with self._lock:
            scored: list[tuple[float, PatternRecord]] = []
            for rec in _records.values():
                if not rec.features or len(rec.features) != len(dna_features):
                    continue
                cos = _cosine_similarity(dna_features, rec.features)
                if cos < _MIN_MATCH_SIMILARITY:
                    continue
                if rec.incident_type != incident_type:
                    cos *= 0.85
                scored.append((cos, rec))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [rec for _, rec in scored[:top_k]]

    def record(
        self,
        fingerprint: str,
        incident_type: str,
        features: list[float],
        root_cause: str = "",
        signal_sequence: list[str] | None = None,
        recommended_steps: list[str] | None = None,
    ) -> PatternRecord:
        """Add or update a pattern record for *fingerprint*.

        On first sight, creates a new record.  On subsequent sightings,
        increments match_count and updates the centroid feature vector.
        Returns the (updated) PatternRecord.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            if fingerprint in self._records:
                rec = self._records[fingerprint]
                rec.match_count += 1
                rec.last_seen = now
                # Update centroid: running average
                n = rec.match_count
                if features and rec.features:
                    rec.features = [
                        (rec.features[i] * (n - 1) + features[i]) / n
                        for i in range(len(features))
                    ]
                # Merge signal sequence (append new signals not yet recorded)
                if signal_sequence:
                    existing = set(rec.signal_sequence)
                    for s in signal_sequence:
                        if s not in existing:
                            rec.signal_sequence.append(s)
                # Merge recommended steps similarly
                if recommended_steps:
                    existing_steps = set(rec.recommended_steps)
                    for s in recommended_steps:
                        if s not in existing_steps:
                            rec.recommended_steps.append(s)
            else:
                rec = PatternRecord(
                    fingerprint=fingerprint,
                    incident_type=incident_type,
                    signal_sequence=list(signal_sequence or []),
                    recommended_steps=list(recommended_steps or []),
                    match_count=1,
                    last_seen=now,
                    features=list(features),
                )
                self._records[fingerprint] = rec
                # Enforce size limit — evict oldest when over cap
                self._evict_if_needed()

            self._save()
            return rec

    def update_outcome(
        self,
        fingerprint: str,
        hypothesis_name: str,
        was_correct: bool,
    ) -> None:
        """Record a correctness signal for a hypothesis under this pattern.

        Used by the learning bus to promote/demote specific hypotheses.
        """
        with self._lock:
            if fingerprint not in self._records:
                return
            rec = self._records[fingerprint]
            if hypothesis_name not in rec.hypothesis_outcomes:
                rec.hypothesis_outcomes[hypothesis_name] = {"correct": 0, "incorrect": 0}
            key = "correct" if was_correct else "incorrect"
            rec.hypothesis_outcomes[hypothesis_name][key] += 1
            self._save()

    def get(self, fingerprint: str) -> PatternRecord | None:
        with self._lock:
            return self._records.get(fingerprint)

    def all_records(self) -> list[PatternRecord]:
        with self._lock:
            return list(self._records.values())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._records = {
                r["fingerprint"]: PatternRecord.from_dict(r)
                for r in raw
                if isinstance(r, dict) and "fingerprint" in r
            }
            logger.info("PatternRegistry loaded %d records from %s", len(self._records), self._path)
        except FileNotFoundError:
            self._records = {}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("PatternRegistry load failed: %s — starting empty", exc)
            self._records = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump([r.to_dict() for r in self._records.values()], f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("PatternRegistry save failed: %s", exc)

    def _evict_if_needed(self) -> None:
        if len(self._records) <= _MAX_RECORDS:
            return
        # Evict the record with the lowest match_count (least-seen pattern)
        oldest_key = min(self._records, key=lambda k: self._records[k].match_count)
        del self._records[oldest_key]


# Module-level singleton (lazy-init, graceful if store missing)
_registry: PatternRegistry | None = None
_registry_lock = threading.Lock()


def get_registry(store_path: str = _DEFAULT_STORE) -> PatternRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = PatternRegistry(store_path)
        return _registry
