"""EvidenceLedger — typed, provenance-aware evidence collection.

Coexists with the legacy ``evidence: dict[str, Any]`` used inside
``supervisor.agent``. Phase 8 ships only the ledger model + bidirectional
adapter; no live wiring into agent.py is done this phase (deferred behind a
feature flag — see ``sentinel_core.evidence.shadow``).

Design choices (driven by the Phase 8A reality scan):

- **NOT a dict subclass.** Conversion to/from the legacy dict is explicit via
  ``to_dict()`` / ``from_dict()``. This prevents accidental adoption from
  silently changing behavior in the supervisor.
- **Insertion-ordered.** Backed by a plain dict (Python 3.7+ preserves order).
- **Replace-on-duplicate.** ``add()`` overwrites an existing key — matches
  the existing ``evidence[k] = v`` semantics.
- **Underscore-prefixed keys preserved.** ``_incident_type``, ``_raw_diff``,
  ``_itsm_change_correlations``, etc. are valid keys; the ledger does not
  filter them.
- **Values stored verbatim.** Nested dicts/lists are kept as-is — no
  defensive copying. Lossless round-trip is the contract.

Dependency rule: stdlib + typing only. No sentinel_core.models imports.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# Provenance enums — captured optionally per item
# ---------------------------------------------------------------------------

class EvidenceSource(str, Enum):
    """Where an evidence item originated."""
    UNKNOWN          = "unknown"
    WORKER           = "worker"             # tool worker output
    PLAYBOOK         = "playbook"           # named playbook step
    POST_PROCESSING  = "post_processing"    # supervisor-injected after collect
    EXPERIENCE       = "experience"         # from episodic memory
    KNOWLEDGE_GRAPH  = "knowledge_graph"    # from KG retrieval
    CORRELATION      = "correlation"        # ITSM / devops / change correlator
    REPLAY           = "replay"             # injected during replay


class EvidenceKind(str, Enum):
    """Broad family of evidence content (taxonomy from Phase 8A scan)."""
    LOGS             = "logs"
    METRICS          = "metrics"
    GOLDEN_SIGNALS   = "golden_signals"
    EVENTS           = "events"
    CHANGES          = "changes"
    APM              = "apm"
    ITSM             = "itsm"
    DEVOPS           = "devops"
    CONFLUENCE       = "confluence"
    HISTORICAL       = "historical"
    CORRELATION      = "correlation"
    PROVENANCE       = "provenance"         # _-prefixed control/metadata keys
    NETWORK          = "network"
    WORKER_RESULT    = "worker_result"      # dynamic playbook-step labels
    OTHER            = "other"


# ---------------------------------------------------------------------------
# Single item
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceItem:
    """One named piece of evidence with optional provenance metadata."""
    key: str
    value: Any
    source: EvidenceSource = EvidenceSource.UNKNOWN
    kind: EvidenceKind = EvidenceKind.OTHER
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0  # auto-stamped by EvidenceLedger.add() if zero

    def to_dict(self) -> dict[str, Any]:
        return {
            "key":        self.key,
            "value":      self.value,
            "source":     self.source.value,
            "kind":       self.kind.value,
            "confidence": self.confidence,
            "metadata":   dict(self.metadata),
            "timestamp":  self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvidenceItem":
        return cls(
            key        = str(d.get("key", "")),
            value      = d.get("value"),
            source     = EvidenceSource(d.get("source", "unknown")),
            kind       = EvidenceKind(d.get("kind", "other")),
            confidence = float(d.get("confidence", 0.0)),
            metadata   = dict(d.get("metadata", {})),
            timestamp  = float(d.get("timestamp", 0.0)),
        )


# ---------------------------------------------------------------------------
# Immutable snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceSnapshot:
    """Point-in-time, immutable view of a ledger.

    Used for replay manifests, workflow checkpoints, and audit comparison.
    The ``items`` tuple is frozen — mutations to the source ledger after a
    snapshot is taken do NOT affect the snapshot.
    """
    items: tuple[EvidenceItem, ...] = ()
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Flatten to a plain key→value dict (drops provenance)."""
        return {item.key: item.value for item in self.items}

    def to_full_dict(self) -> dict[str, dict[str, Any]]:
        """Flatten to key→item-dict, preserving provenance."""
        return {item.key: item.to_dict() for item in self.items}

    def keys(self) -> list[str]:
        return [item.key for item in self.items]

    def __len__(self) -> int:
        return len(self.items)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class EvidenceLedger:
    """Typed, provenance-aware evidence collection.

    Coexists with the legacy ``evidence`` dict in ``supervisor.agent``.
    Conversion happens explicitly via ``to_dict()`` / ``from_dict()`` so the
    ledger never silently replaces the dict on a hot path.
    """

    def __init__(self) -> None:
        # Insertion-ordered. Maps key → EvidenceItem.
        self._items: dict[str, EvidenceItem] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(
        self,
        key: str,
        value: Any,
        *,
        source: EvidenceSource = EvidenceSource.UNKNOWN,
        kind: EvidenceKind = EvidenceKind.OTHER,
        confidence: float = 0.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EvidenceItem:
        """Add or replace an evidence item.

        Replace-on-duplicate semantics match the existing ``evidence[k] = v``
        pattern in supervisor.agent. ``timestamp`` is auto-stamped from
        ``time.time()`` if not supplied via the underlying EvidenceItem.
        """
        if not key:
            raise ValueError("evidence key must be non-empty")
        item = EvidenceItem(
            key=key,
            value=value,
            source=source,
            kind=kind,
            confidence=confidence,
            metadata=dict(metadata or {}),
            timestamp=time.time(),
        )
        self._items[key] = item
        return item

    def merge_dict(
        self,
        data: dict[str, Any],
        *,
        source: EvidenceSource = EvidenceSource.UNKNOWN,
        kind: EvidenceKind = EvidenceKind.OTHER,
    ) -> None:
        """Merge raw dict values into the ledger.

        Each key replaces any existing entry. Useful for absorbing the
        legacy evidence dict at adapter boundaries.
        """
        for k, v in data.items():
            self.add(k, v, source=source, kind=kind)

    def remove(self, key: str) -> bool:
        """Remove a key. Returns True if it existed."""
        return self._items.pop(key, None) is not None

    def clear(self) -> None:
        self._items.clear()

    # ------------------------------------------------------------------
    # Read (dict-like surface)
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the raw value (not the EvidenceItem). Matches dict.get."""
        item = self._items.get(key)
        return item.value if item is not None else default

    def get_item(self, key: str) -> Optional[EvidenceItem]:
        """Return the full EvidenceItem (with provenance) or None."""
        return self._items.get(key)

    def has(self, key: str) -> bool:
        return key in self._items

    def keys(self) -> list[str]:
        return list(self._items.keys())

    def values(self) -> list[Any]:
        """Return raw values in insertion order."""
        return [item.value for item in self._items.values()]

    def items(self) -> list[tuple[str, Any]]:
        """Return (key, raw_value) pairs — matches dict.items() shape."""
        return [(k, item.value) for k, item in self._items.items()]

    def full_items(self) -> list[tuple[str, EvidenceItem]]:
        """Return (key, EvidenceItem) pairs — preserves provenance."""
        return list(self._items.items())

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict (raw values only — drops provenance).

        This is the LOSSY conversion: provenance metadata is discarded. Use
        ``to_full_dict()`` when provenance must survive the round-trip.
        """
        return {k: item.value for k, item in self._items.items()}

    def to_full_dict(self) -> dict[str, dict[str, Any]]:
        """Convert to a key→item-dict mapping, preserving provenance."""
        return {k: item.to_dict() for k, item in self._items.items()}

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        default_source: EvidenceSource = EvidenceSource.UNKNOWN,
        default_kind: EvidenceKind = EvidenceKind.OTHER,
    ) -> "EvidenceLedger":
        """Build a ledger from a plain evidence dict.

        Preserves every key (including underscore-prefixed sentinels) and
        every value verbatim. Default provenance is ``UNKNOWN`` / ``OTHER``
        — callers that know more should override via the kwargs.
        """
        ledger = cls()
        for k, v in data.items():
            ledger.add(k, v, source=default_source, kind=default_kind)
        return ledger

    @classmethod
    def from_full_dict(cls, data: dict[str, dict[str, Any]]) -> "EvidenceLedger":
        """Rehydrate a ledger from a previously-serialized full dict.

        Restores provenance metadata exactly. The reverse of ``to_full_dict()``.
        """
        ledger = cls()
        for k, item_dict in data.items():
            # Ensure the embedded key matches the dict key
            if not item_dict.get("key"):
                item_dict = {**item_dict, "key": k}
            ledger._items[k] = EvidenceItem.from_dict(item_dict)
        return ledger

    def snapshot(self) -> EvidenceSnapshot:
        """Return an immutable snapshot at this point in time."""
        return EvidenceSnapshot(
            items=tuple(self._items.values()),
            created_at=time.time(),
        )

    # ------------------------------------------------------------------
    # Equality (value-based; ignores timestamps)
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EvidenceLedger):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return f"EvidenceLedger(keys={list(self._items.keys())!r})"


__all__ = [
    "EvidenceSource",
    "EvidenceKind",
    "EvidenceItem",
    "EvidenceSnapshot",
    "EvidenceLedger",
]


# Suppress unused-import warning for replace; reserved for future use in
# EvidenceItem.with_metadata() (not needed in Phase 8 surface).
_ = replace
