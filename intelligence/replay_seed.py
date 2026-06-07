"""ReplaySeed — structured, durable investigation replay storage.

Replaces the ephemeral /tmp/sentinalai_replays with a content-versioned,
forward-compatible seed that supports:
  - Full investigation reconstruction
  - Timeline reconstruction
  - Evidence reconstruction
  - RCA reconstruction
  - Future AARC / Executive Replay compatibility

Persisted to eval/investigations/{investigation_id}_replay.json (atomic write).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.schema import SCHEMA_VERSION, new_id

logger = logging.getLogger("sentinalai.intelligence.replay_seed")

_DEFAULT_DIR = os.getenv("INVESTIGATIONS_DIR", "eval/investigations")


@dataclass
class ReplaySeed:
    seed_id:                  str
    replay_seed_id:           str          # globally unique alias (same as seed_id, distinct field for AARC compat)
    investigation_id:         str
    incident_id:              str
    incident_snapshot:        dict[str, Any]   # incident at investigation time
    evidence_graph_snapshot:  dict[str, Any]   # EvidenceGraph.to_dict()
    tool_call_sequence:       list[dict]        # ordered receipts
    rca_report_snapshot:      dict[str, Any]
    decision_traces:          list[dict]
    resolution_outcome:       dict[str, Any] | None
    schema_version:           str
    created_at:               str
    aarc_compatible:          bool = True
    extras:                   dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def make(
        cls,
        investigation_id: str,
        incident_id: str,
        incident_snapshot: dict[str, Any],
        evidence_graph_snapshot: dict[str, Any],
        tool_call_sequence: list[dict] | None = None,
        rca_report_snapshot: dict[str, Any] | None = None,
        decision_traces: list[dict] | None = None,
        resolution_outcome: dict[str, Any] | None = None,
    ) -> "ReplaySeed":
        now = datetime.now(timezone.utc).isoformat()
        seed_id = new_id("replay", investigation_id, now)
        return cls(
            seed_id=seed_id,
            replay_seed_id=seed_id,
            investigation_id=investigation_id,
            incident_id=incident_id,
            incident_snapshot=incident_snapshot,
            evidence_graph_snapshot=evidence_graph_snapshot,
            tool_call_sequence=tool_call_sequence or [],
            rca_report_snapshot=rca_report_snapshot or {},
            decision_traces=decision_traces or [],
            resolution_outcome=resolution_outcome,
            schema_version=SCHEMA_VERSION,
            created_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seed_id":                 self.seed_id,
            "replay_seed_id":          self.replay_seed_id,
            "investigation_id":        self.investigation_id,
            "incident_id":             self.incident_id,
            "incident_snapshot":       self.incident_snapshot,
            "evidence_graph_snapshot": self.evidence_graph_snapshot,
            "tool_call_sequence":      self.tool_call_sequence,
            "rca_report_snapshot":     self.rca_report_snapshot,
            "decision_traces":         self.decision_traces,
            "resolution_outcome":      self.resolution_outcome,
            "schema_version":          self.schema_version,
            "created_at":              self.created_at,
            "aarc_compatible":         self.aarc_compatible,
        }
        d.update(self.extras)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplaySeed":
        known = {
            "seed_id", "replay_seed_id", "investigation_id", "incident_id",
            "incident_snapshot", "evidence_graph_snapshot", "tool_call_sequence",
            "rca_report_snapshot", "decision_traces", "resolution_outcome",
            "schema_version", "created_at", "aarc_compatible",
        }
        return cls(
            seed_id=d["seed_id"],
            replay_seed_id=d.get("replay_seed_id", d["seed_id"]),
            investigation_id=d["investigation_id"],
            incident_id=d.get("incident_id", ""),
            incident_snapshot=d.get("incident_snapshot", {}),
            evidence_graph_snapshot=d.get("evidence_graph_snapshot", {}),
            tool_call_sequence=d.get("tool_call_sequence", []),
            rca_report_snapshot=d.get("rca_report_snapshot", {}),
            decision_traces=d.get("decision_traces", []),
            resolution_outcome=d.get("resolution_outcome"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            created_at=d.get("created_at", ""),
            aarc_compatible=d.get("aarc_compatible", True),
            extras={k: v for k, v in d.items() if k not in known},
        )


class ReplaySeedStore:
    """Persistent durable replay seed storage. One file per investigation."""

    def __init__(self, investigations_dir: str = _DEFAULT_DIR) -> None:
        self._dir = investigations_dir
        self._lock = threading.Lock()

    def _path(self, investigation_id: str) -> str:
        return os.path.join(self._dir, f"{investigation_id}_replay.json")

    def save(self, seed: ReplaySeed) -> str:
        """Persist seed atomically. Returns path."""
        path = self._path(seed.investigation_id)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            tmp = path + ".tmp"
            with self._lock:
                with open(tmp, "w") as f:
                    json.dump(seed.to_dict(), f, indent=2)
                os.replace(tmp, path)
        except OSError as exc:
            logger.debug("ReplaySeedStore.save failed (non-critical): %s", exc)
        return path

    def load(self, investigation_id: str) -> ReplaySeed | None:
        """Load seed for an investigation. Returns None if not found."""
        try:
            with open(self._path(investigation_id)) as f:
                return ReplaySeed.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.debug("ReplaySeedStore.load(%s): %s", investigation_id, exc)
            return None

    def exists(self, investigation_id: str) -> bool:
        return os.path.exists(self._path(investigation_id))
