"""LearningCycle — one deterministic pass over the corpus."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sentinel_core.continuous_learning.feedback_collector import (
    FeedbackCollector,
    FeedbackSignal,
)
from sentinel_core.continuous_learning.learning_engine import (
    LEARNING_SCHEMA_VERSION,
    LearningEngine,
    LearningScores,
)
from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.models._immutable import freeze_dict


@dataclass(frozen=True)
class LearningSnapshot:
    snapshot_id:      str
    generated_at:     str = ""
    sequence:         int = 0
    corpus_size:      int = 0
    signal_count:     int = 0
    scores:           LearningScores = field(default_factory=LearningScores)
    metadata:         dict[str, Any] = field(default_factory=dict)
    schema_version:   int = LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # RC-D: prevent mutation of the metadata dict via attribute access.
        object.__setattr__(self, "metadata", freeze_dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":   self.schema_version,
            "snapshot_id":      self.snapshot_id,
            "generated_at":     self.generated_at,
            "sequence":         int(self.sequence),
            "corpus_size":      int(self.corpus_size),
            "signal_count":     int(self.signal_count),
            "scores":           self.scores.to_dict(),
            "metadata":         dict(self.metadata),
        }


def _make_snapshot_id(corpus_ids: tuple[str, ...], sequence: int) -> str:
    raw = f"seq={sequence}|" + ",".join(corpus_ids)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class LearningCycle:
    """One deterministic learning cycle. Append-only by convention:
    callers accumulate snapshots externally; this class never mutates
    its inputs."""

    def __init__(self, engine: LearningEngine | None = None) -> None:
        self._engine = engine or LearningEngine()

    def run(
        self,
        records: Iterable[MemoryRecord],
        feedback: FeedbackCollector | None = None,
        *,
        generated_at: str = "",
        sequence: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> LearningSnapshot:
        records = tuple(records or ())
        signals = feedback.all() if feedback else ()
        scores = self._engine.scores(records, feedback)
        snapshot_id = _make_snapshot_id(
            tuple(sorted(r.memory_id for r in records)),
            int(sequence),
        )
        return LearningSnapshot(
            snapshot_id=snapshot_id,
            generated_at=str(generated_at),
            sequence=int(sequence),
            corpus_size=len(records),
            signal_count=len(signals),
            scores=scores,
            metadata=dict(metadata or {}),
        )


__all__ = ["LearningSnapshot", "LearningCycle"]
