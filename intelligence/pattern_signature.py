"""PatternSignature — recurring graph structure fingerprints.

Extends PatternRegistry (16-dim feature vector) with:
  - Graph-structural fingerprint (node_type_sequence + edge_relationship_sequence)
  - Outcome-linked success/failure rates
  - Universal vs. service-scoped patterns

Pattern ID is deterministic from graph structure, enabling deduplication.
Persisted to eval/pattern_signatures.json (atomic write).
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.schema import new_id

logger = logging.getLogger("sentinalai.intelligence.pattern_signature")

_DEFAULT_PATH  = os.getenv("PATTERN_SIGNATURES_PATH", "eval/pattern_signatures.json")
_MAX_PATTERNS  = int(os.getenv("PATTERN_SIGNATURES_MAX", "1000"))
_MATCH_THRESHOLD = float(os.getenv("PATTERN_MATCH_THRESHOLD", "0.72"))
_MIN_FREQUENCY = int(os.getenv("PATTERN_MIN_FREQUENCY", "2"))


@dataclass
class PatternSignature:
    pattern_id:                  str
    node_type_sequence:          list[str]
    edge_relationship_sequence:  list[str]
    service_scope:               str | None      # None = universal
    incident_types:              list[str]
    frequency:                   int             # total observations
    success_count:               int             # SUCCESS outcomes
    failure_count:               int             # FAILED outcomes
    confidence:                  float
    last_seen:                   str
    first_seen:                  str
    matching_resolutions:        list[str]       # actions that worked
    features:                    list[float]     # 16-dim (PatternRegistry compat)

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return round(self.success_count / total, 3) if total else 0.0

    @property
    def failure_rate(self) -> float:
        total = self.success_count + self.failure_count
        return round(self.failure_count / total, 3) if total else 0.0

    @classmethod
    def from_graph_structure(
        cls,
        node_types: list[str],
        edge_rels: list[str],
        incident_type: str,
        service_scope: str | None = None,
        features: list[float] | None = None,
    ) -> "PatternSignature":
        raw = "|".join(sorted(node_types) + sorted(edge_rels) + [incident_type])
        pattern_id = new_id(raw)
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            pattern_id=pattern_id,
            node_type_sequence=node_types,
            edge_relationship_sequence=edge_rels,
            service_scope=service_scope,
            incident_types=[incident_type],
            frequency=0,
            success_count=0,
            failure_count=0,
            confidence=0.0,
            last_seen=now,
            first_seen=now,
            matching_resolutions=[],
            features=features or [0.0] * 16,
        )

    def record_occurrence(
        self,
        resolution_status: str | None = None,
        resolution_action: str | None = None,
        incident_type: str | None = None,
    ) -> None:
        self.frequency += 1
        self.last_seen = datetime.now(timezone.utc).isoformat()
        if incident_type and incident_type not in self.incident_types:
            self.incident_types.append(incident_type)
        if resolution_status == "SUCCESS":
            self.success_count += 1
        elif resolution_status == "FAILED":
            self.failure_count += 1
        if resolution_action and resolution_action not in self.matching_resolutions:
            self.matching_resolutions.append(resolution_action)
        # Update confidence: higher frequency + success rate → higher confidence
        freq_factor = min(1.0, math.log(self.frequency + 1) / math.log(50))
        self.confidence = round(freq_factor * (0.5 + 0.5 * self.success_rate), 3)

    def similarity(self, other_node_types: list[str], other_edge_rels: list[str]) -> float:
        """Jaccard similarity on node types + edge relationships."""
        a = set(self.node_type_sequence) | set(self.edge_relationship_sequence)
        b = set(other_node_types) | set(other_edge_rels)
        if not a and not b:
            return 1.0
        return len(a & b) / len(a | b) if (a | b) else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id":                 self.pattern_id,
            "node_type_sequence":         self.node_type_sequence,
            "edge_relationship_sequence": self.edge_relationship_sequence,
            "service_scope":              self.service_scope,
            "incident_types":             self.incident_types,
            "frequency":                  self.frequency,
            "success_count":              self.success_count,
            "failure_count":              self.failure_count,
            "success_rate":               self.success_rate,
            "failure_rate":               self.failure_rate,
            "confidence":                 self.confidence,
            "last_seen":                  self.last_seen,
            "first_seen":                 self.first_seen,
            "matching_resolutions":       self.matching_resolutions,
            "features":                   self.features,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PatternSignature":
        return cls(
            pattern_id=d["pattern_id"],
            node_type_sequence=d.get("node_type_sequence", []),
            edge_relationship_sequence=d.get("edge_relationship_sequence", []),
            service_scope=d.get("service_scope"),
            incident_types=d.get("incident_types", []),
            frequency=d.get("frequency", 0),
            success_count=d.get("success_count", 0),
            failure_count=d.get("failure_count", 0),
            confidence=float(d.get("confidence", 0.0)),
            last_seen=d.get("last_seen", ""),
            first_seen=d.get("first_seen", ""),
            matching_resolutions=d.get("matching_resolutions", []),
            features=d.get("features", [0.0] * 16),
        )


class PatternSignatureIndex:
    """Persistent pattern library. Thread-safe.

    Matches current graph structure against known patterns.
    Ranked by: confidence × similarity score.
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._patterns: dict[str, PatternSignature] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._patterns = {k: PatternSignature.from_dict(v) for k, v in data.items()}
        except (FileNotFoundError, json.JSONDecodeError):
            self._patterns = {}
        self._loaded = True

    def match(
        self,
        node_types: list[str],
        edge_rels: list[str],
        incident_type: str,
        service: str | None = None,
        top_k: int = 3,
    ) -> list[PatternSignature]:
        """Find top-k matching patterns. Returns [] if none above threshold."""
        with self._lock:
            self._ensure_loaded()
            scored: list[tuple[float, PatternSignature]] = []
            for p in self._patterns.values():
                if p.frequency < _MIN_FREQUENCY:
                    continue
                # Prefer service-scoped patterns that match this service
                scope_bonus = 0.1 if (p.service_scope and p.service_scope == service) else 0.0
                type_bonus  = 0.1 if incident_type in p.incident_types else 0.0
                sim = p.similarity(node_types, edge_rels)
                combined = sim * p.confidence + scope_bonus + type_bonus
                if sim >= _MATCH_THRESHOLD:
                    scored.append((combined, p))
            scored.sort(key=lambda x: -x[0])
            return [p for _, p in scored[:top_k]]

    def upsert(self, pattern: PatternSignature) -> None:
        with self._lock:
            self._ensure_loaded()
            self._patterns[pattern.pattern_id] = pattern
            self._evict_if_needed()
            self._flush_locked()

    def get(self, pattern_id: str) -> PatternSignature | None:
        with self._lock:
            self._ensure_loaded()
            return self._patterns.get(pattern_id)

    def all_patterns(self) -> list[PatternSignature]:
        with self._lock:
            self._ensure_loaded()
            return list(self._patterns.values())

    def _evict_if_needed(self) -> None:
        if len(self._patterns) <= _MAX_PATTERNS:
            return
        # Evict lowest-frequency patterns first
        sorted_ids = sorted(self._patterns, key=lambda k: self._patterns[k].frequency)
        for pid in sorted_ids[:len(self._patterns) - _MAX_PATTERNS]:
            del self._patterns[pid]

    def _flush_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
            tmp = self._path + ".tmp"
            data = {k: v.to_dict() for k, v in self._patterns.items()}
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.debug("PatternSignatureIndex flush failed: %s", exc)
