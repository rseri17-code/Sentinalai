"""HypothesisGraph — immutable container for an investigation's
hypothesis lifecycle.

Zero side effects. Same input → byte-identical output.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sentinel_core.hypotheses.schemas import (
    Hypothesis,
    HypothesisStatus,
    HYPOTHESIS_SCHEMA_VERSION,
    _tuples_to_lists,
)


@dataclass(frozen=True)
class HypothesisGraph:
    """Immutable snapshot of an investigation's hypothesis space."""
    investigation_id: str = ""
    hypotheses:       tuple[Hypothesis, ...] = ()
    started_at:       str = ""
    completed_at:     str = ""
    schema_version:   int = HYPOTHESIS_SCHEMA_VERSION

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def count(self) -> int:
        return len(self.hypotheses)

    def get(self, hypothesis_id: str) -> Hypothesis | None:
        for h in self.hypotheses:
            if h.hypothesis_id == hypothesis_id:
                return h
        return None

    def by_status(self, status: str | HypothesisStatus) -> tuple[Hypothesis, ...]:
        s = status.value if isinstance(status, HypothesisStatus) else str(status)
        return tuple(h for h in self.hypotheses if h.status == s)

    def confirmed(self) -> tuple[Hypothesis, ...]:
        return self.by_status(HypothesisStatus.CONFIRMED)

    def ruled_out(self) -> tuple[Hypothesis, ...]:
        return self.by_status(HypothesisStatus.RULED_OUT)

    def supported(self) -> tuple[Hypothesis, ...]:
        return self.by_status(HypothesisStatus.SUPPORTED)

    def refuted(self) -> tuple[Hypothesis, ...]:
        return self.by_status(HypothesisStatus.REFUTED)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version":   self.schema_version,
            "investigation_id": self.investigation_id,
            "started_at":       self.started_at,
            "completed_at":     self.completed_at,
            "count":            len(self.hypotheses),
            "hypotheses":       [h.to_dict() for h in sorted(
                self.hypotheses, key=lambda x: x.hypothesis_id
            )],
        }


__all__ = ["HypothesisGraph"]
