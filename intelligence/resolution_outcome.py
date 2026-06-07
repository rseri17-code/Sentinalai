"""ResolutionOutcome — Requirement 28.

Store what was recommended, what was executed, and whether it worked.
This closes the learning loop: patterns earn success_rate from outcomes.

Persistence: eval/resolution_outcomes.jsonl (append-only NDJSON).
Thread-safe. Forward-compatible (extra fields preserved).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.schema import ResolutionStatus, new_id

logger = logging.getLogger("sentinalai.intelligence.resolution_outcome")

_DEFAULT_PATH = os.getenv("RESOLUTION_OUTCOMES_PATH", "eval/resolution_outcomes.jsonl")
_lock = threading.Lock()


@dataclass
class ResolutionOutcome:
    outcome_id:           str
    investigation_id:     str
    incident_id:          str
    service_id:           str                # service name (CMDB ID when available)
    application_id:       str                # app grouping (empty until CMDB integrated)
    pattern_signature_id: str                # matched PatternSignature, if any
    recommendation_id:    str                # which recommendation was made
    recommended_action:   str
    executed_action:      str
    resolution_status:    ResolutionStatus
    mttr_minutes:         float
    resolution_timestamp: str
    operator_feedback:    str
    operator_notes:       str
    created_at:           str
    extras:               dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def make(
        cls,
        investigation_id: str,
        incident_id: str,
        service_id: str,
        executed_action: str,
        resolution_status: ResolutionStatus | str,
        mttr_minutes: float = 0.0,
        recommended_action: str = "",
        resolution_timestamp: str = "",
        operator_feedback: str = "",
        operator_notes: str = "",
        application_id: str = "",
        pattern_signature_id: str = "",
        recommendation_id: str = "",
    ) -> "ResolutionOutcome":
        ts = resolution_timestamp or datetime.now(timezone.utc).isoformat()
        status = ResolutionStatus(resolution_status) if isinstance(resolution_status, str) else resolution_status
        return cls(
            outcome_id=new_id(investigation_id, executed_action, ts),
            investigation_id=investigation_id,
            incident_id=incident_id,
            service_id=service_id,
            application_id=application_id,
            pattern_signature_id=pattern_signature_id,
            recommendation_id=recommendation_id,
            recommended_action=recommended_action,
            executed_action=executed_action,
            resolution_status=status,
            mttr_minutes=mttr_minutes,
            resolution_timestamp=ts,
            operator_feedback=operator_feedback,
            operator_notes=operator_notes,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "outcome_id":           self.outcome_id,
            "investigation_id":     self.investigation_id,
            "incident_id":          self.incident_id,
            "service_id":           self.service_id,
            "application_id":       self.application_id,
            "pattern_signature_id": self.pattern_signature_id,
            "recommendation_id":    self.recommendation_id,
            "recommended_action":   self.recommended_action,
            "executed_action":      self.executed_action,
            "resolution_status":    self.resolution_status.value,
            "mttr_minutes":         self.mttr_minutes,
            "resolution_timestamp": self.resolution_timestamp,
            "operator_feedback":    self.operator_feedback,
            "operator_notes":       self.operator_notes,
            "created_at":           self.created_at,
        }
        d.update(self.extras)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ResolutionOutcome":
        known = {
            "outcome_id", "investigation_id", "incident_id", "service_id",
            "application_id", "pattern_signature_id", "recommendation_id",
            "recommended_action", "executed_action", "resolution_status",
            "mttr_minutes", "resolution_timestamp", "operator_feedback",
            "operator_notes", "created_at",
        }
        return cls(
            outcome_id=d["outcome_id"],
            investigation_id=d["investigation_id"],
            incident_id=d.get("incident_id", ""),
            service_id=d.get("service_id", ""),
            application_id=d.get("application_id", ""),
            pattern_signature_id=d.get("pattern_signature_id", ""),
            recommendation_id=d.get("recommendation_id", ""),
            recommended_action=d.get("recommended_action", ""),
            executed_action=d.get("executed_action", ""),
            resolution_status=ResolutionStatus(d.get("resolution_status", ResolutionStatus.FAILED.value)),
            mttr_minutes=float(d.get("mttr_minutes", 0.0)),
            resolution_timestamp=d.get("resolution_timestamp", ""),
            operator_feedback=d.get("operator_feedback", ""),
            operator_notes=d.get("operator_notes", ""),
            created_at=d.get("created_at", ""),
            extras={k: v for k, v in d.items() if k not in known},
        )


class OutcomeStore:
    """Append-only store for resolution outcomes. Thread-safe."""

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(self, outcome: ResolutionOutcome) -> None:
        """Append outcome to NDJSON log."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
            with self._lock:
                with open(self._path, "a") as f:
                    f.write(json.dumps(outcome.to_dict()) + "\n")
        except OSError as exc:
            logger.debug("OutcomeStore.record failed (non-critical): %s", exc)

    def load_all(self, last_n: int = 1000) -> list[ResolutionOutcome]:
        """Load last N outcomes. Returns [] if file absent."""
        try:
            with open(self._path) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            return [ResolutionOutcome.from_dict(json.loads(ln)) for ln in lines[-last_n:]]
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def load_for_service(self, service_id: str, last_n: int = 100) -> list[ResolutionOutcome]:
        return [o for o in self.load_all(last_n=5000) if o.service_id == service_id][-last_n:]

    def load_for_investigation(self, investigation_id: str) -> list[ResolutionOutcome]:
        return [o for o in self.load_all() if o.investigation_id == investigation_id]

    def success_rate_for_action(self, executed_action: str, min_samples: int = 3) -> float | None:
        """Return success rate for an action across all outcomes. None if insufficient data."""
        matching = [o for o in self.load_all() if o.executed_action == executed_action]
        if len(matching) < min_samples:
            return None
        successes = sum(1 for o in matching if o.resolution_status == ResolutionStatus.SUCCESS)
        return round(successes / len(matching), 3)


# Module-level singleton
_store: OutcomeStore | None = None
_store_lock = threading.Lock()


def get_outcome_store() -> OutcomeStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = OutcomeStore()
        return _store
