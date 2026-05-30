"""Canonical output schema for all SentinalAI workers.

WorkerResult provides a typed wrapper around raw worker output so
downstream evidence extraction is not fragile to result shape variation.

Workers are not required to return a WorkerResult — raw dicts are still
accepted everywhere — but producing one allows the grounding layer to
attach provenance metadata without guessing at dict key names.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class WorkerResult:
    """Canonical evidence output from a single worker call.

    Attributes:
        source:          Worker name (e.g. "log_worker", "metrics_worker").
        entity:          Service or entity the evidence was collected for.
        evidence_type:   Category of evidence ("logs", "metrics", "traces",
                         "topology", "config", "change", "incident").
        data:            Raw result payload from the worker.
        confidence_hint: Optional 0.0–1.0 quality estimate from the worker
                         (e.g. Sysdig golden-signal anomaly score).
        query:           The raw query executed, for replay / debugging.
        time_window_start: ISO8601 start of the evidence window.
        time_window_end:   ISO8601 end of the evidence window.
    """

    source: str
    entity: str
    evidence_type: str
    data: dict = field(default_factory=dict)
    confidence_hint: float | None = None
    query: str = ""
    time_window_start: str = ""
    time_window_end: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkerResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
