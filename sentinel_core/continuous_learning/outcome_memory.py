"""Append-only outcome ledger."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sentinel_core.continuous_learning.feedback_collector import FeedbackSignal


@dataclass(frozen=True)
class OutcomeRecord:
    memory_id:              str
    incident_id:            str = ""
    predicted_root_cause:   str = ""
    verified_root_cause:    str = ""
    predicted_confidence:   int = 0
    verified_confidence:    int = 0
    predicted_mtti_ms:      int = 0
    verified_mtti_ms:       int = 0
    replay_agreement:       float = 0.0
    benchmark_agreement:    float = 0.0
    feedback_signals:       tuple[FeedbackSignal, ...] = ()
    recorded_at:            str = ""
    schema_version:         int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["feedback_signals"] = [s for s in d["feedback_signals"]]
        d["replay_agreement"] = round(float(d["replay_agreement"]), 4)
        d["benchmark_agreement"] = round(float(d["benchmark_agreement"]), 4)
        return d


class OutcomeMemory:
    """Append-only ledger of :class:`OutcomeRecord` objects.

    Never mutates previous entries. ``add`` returns a new instance.
    """

    def __init__(self, records: tuple[OutcomeRecord, ...] = ()) -> None:
        self._records = tuple(records)

    def add(self, record: OutcomeRecord) -> "OutcomeMemory":
        return OutcomeMemory(self._records + (record,))

    def add_many(self, records: Iterable[OutcomeRecord]) -> "OutcomeMemory":
        return OutcomeMemory(self._records + tuple(records))

    def by_memory_id(self, memory_id: str) -> tuple[OutcomeRecord, ...]:
        return tuple(r for r in self._records if r.memory_id == memory_id)

    def all(self) -> tuple[OutcomeRecord, ...]:
        return self._records

    def __len__(self) -> int:
        return len(self._records)


__all__ = ["OutcomeRecord", "OutcomeMemory"]
