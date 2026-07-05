"""Feedback collection — deterministic ingestion of operator + replay +
benchmark signals. Immutable + append-only."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


class FeedbackSource(str, Enum):
    OPERATOR  = "operator"
    REPLAY    = "replay"
    BENCHMARK = "benchmark"
    SYSTEM    = "system"


class FeedbackKind(str, Enum):
    ROOT_CAUSE_CORRECT          = "root_cause_correct"
    ROOT_CAUSE_INCORRECT        = "root_cause_incorrect"
    ROOT_CAUSE_PARTIAL          = "root_cause_partial"
    FALSE_POSITIVE              = "false_positive"
    FALSE_NEGATIVE              = "false_negative"
    RESOLUTION_CONFIRMED        = "resolution_confirmed"
    RESOLUTION_REJECTED         = "resolution_rejected"
    MTTI_OVERRIDE               = "mtti_override"
    CONFIDENCE_OVERRIDE         = "confidence_override"
    HYPOTHESIS_ACCEPTED         = "hypothesis_accepted"
    HYPOTHESIS_REJECTED         = "hypothesis_rejected"
    STRATEGY_APPROVED           = "strategy_approved"
    STRATEGY_REJECTED           = "strategy_rejected"


@dataclass(frozen=True)
class FeedbackSignal:
    memory_id:    str
    source:       str
    kind:         str
    value:        Any = None
    timestamp:    str = ""            # caller-supplied ISO 8601
    notes:        str = ""
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeedbackCollector:
    """Immutable-by-convention ingestion of feedback signals.

    ``add`` returns a NEW collector with the appended signal; the
    original instance is never mutated.
    """

    def __init__(self, signals: tuple[FeedbackSignal, ...] = ()) -> None:
        self._signals = tuple(signals)

    def add(self, signal: FeedbackSignal) -> "FeedbackCollector":
        return FeedbackCollector(self._signals + (signal,))

    def add_many(self, signals: Iterable[FeedbackSignal]) -> "FeedbackCollector":
        return FeedbackCollector(self._signals + tuple(signals))

    def by_memory_id(self, memory_id: str) -> tuple[FeedbackSignal, ...]:
        return tuple(s for s in self._signals if s.memory_id == memory_id)

    def by_source(self, source: str) -> tuple[FeedbackSignal, ...]:
        return tuple(s for s in self._signals if s.source == source)

    def by_kind(self, kind: str) -> tuple[FeedbackSignal, ...]:
        return tuple(s for s in self._signals if s.kind == kind)

    def all(self) -> tuple[FeedbackSignal, ...]:
        return self._signals

    def __len__(self) -> int:
        return len(self._signals)


__all__ = [
    "FeedbackSource",
    "FeedbackKind",
    "FeedbackSignal",
    "FeedbackCollector",
]
