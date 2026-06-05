"""DecisionTrace — immutable record of why each decision was made.

Every hypothesis, recommendation, gate verdict, and step selection
must be traceable to: supporting evidence, a pattern, and historical success rate.

Persisted per-investigation as NDJSON: eval/investigations/{id}_decisions.jsonl
Thread-safe append-only writes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.schema import new_id

logger = logging.getLogger("sentinalai.intelligence.decision_trace")

_DEFAULT_DIR = os.getenv("INVESTIGATIONS_DIR", "eval/investigations")


@dataclass
class DecisionTrace:
    trace_id:               str
    investigation_id:       str
    decision_type:          str          # hypothesis|recommendation|gate_verdict|step_selection
    decision:               str          # what was decided
    supporting_evidence:    list[dict]   # [{node_id, source_type, content_excerpt, confidence}]
    contradicting_evidence: list[dict]
    pattern_id:             str | None
    pattern_frequency:      int
    pattern_success_rate:   float
    prior_occurrence_count: int
    historical_success_rate: float
    confidence:             float
    reasoning_path:         list[str]    # ordered reasoning steps
    why:                    str          # human-readable causal explanation
    created_at:             str
    extras:                 dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def make(
        cls,
        investigation_id: str,
        decision_type: str,
        decision: str,
        why: str,
        confidence: float,
        supporting_evidence: list[dict] | None = None,
        contradicting_evidence: list[dict] | None = None,
        reasoning_path: list[str] | None = None,
        pattern_id: str | None = None,
        pattern_frequency: int = 0,
        pattern_success_rate: float = 0.0,
        prior_occurrence_count: int = 0,
        historical_success_rate: float = 0.0,
    ) -> "DecisionTrace":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            trace_id=new_id(investigation_id, decision_type, decision[:40], now),
            investigation_id=investigation_id,
            decision_type=decision_type,
            decision=decision,
            supporting_evidence=supporting_evidence or [],
            contradicting_evidence=contradicting_evidence or [],
            pattern_id=pattern_id,
            pattern_frequency=pattern_frequency,
            pattern_success_rate=pattern_success_rate,
            prior_occurrence_count=prior_occurrence_count,
            historical_success_rate=historical_success_rate,
            confidence=confidence,
            reasoning_path=reasoning_path or [],
            why=why,
            created_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "trace_id":               self.trace_id,
            "investigation_id":       self.investigation_id,
            "decision_type":          self.decision_type,
            "decision":               self.decision,
            "supporting_evidence":    self.supporting_evidence,
            "contradicting_evidence": self.contradicting_evidence,
            "pattern_id":             self.pattern_id,
            "pattern_frequency":      self.pattern_frequency,
            "pattern_success_rate":   self.pattern_success_rate,
            "prior_occurrence_count": self.prior_occurrence_count,
            "historical_success_rate": self.historical_success_rate,
            "confidence":             round(self.confidence, 4),
            "reasoning_path":         self.reasoning_path,
            "why":                    self.why,
            "created_at":             self.created_at,
        }
        d.update(self.extras)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DecisionTrace":
        known = {
            "trace_id", "investigation_id", "decision_type", "decision",
            "supporting_evidence", "contradicting_evidence", "pattern_id",
            "pattern_frequency", "pattern_success_rate", "prior_occurrence_count",
            "historical_success_rate", "confidence", "reasoning_path", "why", "created_at",
        }
        return cls(
            trace_id=d["trace_id"],
            investigation_id=d["investigation_id"],
            decision_type=d.get("decision_type", ""),
            decision=d.get("decision", ""),
            supporting_evidence=d.get("supporting_evidence", []),
            contradicting_evidence=d.get("contradicting_evidence", []),
            pattern_id=d.get("pattern_id"),
            pattern_frequency=d.get("pattern_frequency", 0),
            pattern_success_rate=float(d.get("pattern_success_rate", 0.0)),
            prior_occurrence_count=d.get("prior_occurrence_count", 0),
            historical_success_rate=float(d.get("historical_success_rate", 0.0)),
            confidence=float(d.get("confidence", 0.0)),
            reasoning_path=d.get("reasoning_path", []),
            why=d.get("why", ""),
            created_at=d.get("created_at", ""),
            extras={k: v for k, v in d.items() if k not in known},
        )


class DecisionTraceLog:
    """Append-only log of decision traces per investigation."""

    def __init__(self, investigations_dir: str = _DEFAULT_DIR) -> None:
        self._dir = investigations_dir
        self._lock = threading.Lock()

    def _path(self, investigation_id: str) -> str:
        return os.path.join(self._dir, f"{investigation_id}_decisions.jsonl")

    def append(self, trace: DecisionTrace) -> None:
        """Append one trace. Never raises; logs on failure."""
        try:
            path = self._path(trace.investigation_id)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with self._lock:
                with open(path, "a") as f:
                    f.write(json.dumps(trace.to_dict()) + "\n")
        except OSError as exc:
            logger.debug("DecisionTraceLog.append failed (non-critical): %s", exc)

    def load(self, investigation_id: str) -> list[DecisionTrace]:
        """Load all traces for an investigation."""
        try:
            with open(self._path(investigation_id)) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            return [DecisionTrace.from_dict(json.loads(ln)) for ln in lines]
        except (FileNotFoundError, json.JSONDecodeError):
            return []
