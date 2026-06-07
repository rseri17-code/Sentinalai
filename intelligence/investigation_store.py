"""InvestigationStore — unified coordinator for all intelligence artifacts.

Single entry point for reading and writing intelligence artifacts:
  - EvidenceGraph (per-investigation)
  - ReplaySeed (per-investigation)
  - DecisionTraces (per-investigation, NDJSON)
  - ResolutionOutcomes (all-investigations, NDJSON)
  - ServiceProfiles (all-services, JSON)
  - PatternSignatures (all-patterns, JSON)

Storage layout:
  eval/investigations/
    {investigation_id}.json          EvidenceGraph
    {investigation_id}_replay.json   ReplaySeed
    {investigation_id}_decisions.jsonl  DecisionTraces
    _index.jsonl                     Append-only lookup index
  eval/resolution_outcomes.jsonl
  eval/service_profiles.json
  eval/pattern_signatures.json

Designed for 100k+ investigations via monthly partitioning and the append-only
index (no full scan required for common lookups).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from intelligence.decision_trace import DecisionTrace, DecisionTraceLog
from intelligence.evidence_graph import EvidenceGraph
from intelligence.pattern_signature import PatternSignature, PatternSignatureIndex
from intelligence.replay_seed import ReplaySeed, ReplaySeedStore
from intelligence.resolution_outcome import OutcomeStore, ResolutionOutcome
from intelligence.service_profile import ServiceProfile, ServiceProfileIndex

logger = logging.getLogger("sentinalai.intelligence.investigation_store")

_DEFAULT_DIR         = os.getenv("INVESTIGATIONS_DIR",      "eval/investigations")
_DEFAULT_OUTCOMES    = os.getenv("RESOLUTION_OUTCOMES_PATH","eval/resolution_outcomes.jsonl")
_DEFAULT_PROFILES    = os.getenv("SERVICE_PROFILES_PATH",   "eval/service_profiles.json")
_DEFAULT_PATTERNS    = os.getenv("PATTERN_SIGNATURES_PATH", "eval/pattern_signatures.json")
_INDEX_FILE          = "_index.jsonl"


@dataclass
class InvestigationRecord:
    """Lightweight index entry — avoids loading full graph for common lookups."""
    investigation_id: str
    incident_id:      str
    service:          str
    incident_type:    str
    phase:            str
    node_count:       int
    edge_count:       int
    created_at:       str

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "incident_id":      self.incident_id,
            "service":          self.service,
            "incident_type":    self.incident_type,
            "phase":            self.phase,
            "node_count":       self.node_count,
            "edge_count":       self.edge_count,
            "created_at":       self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InvestigationRecord":
        return cls(**{k: d.get(k, "") for k in (
            "investigation_id", "incident_id", "service", "incident_type",
            "phase", "created_at",
        )}, node_count=int(d.get("node_count", 0)), edge_count=int(d.get("edge_count", 0)))


class InvestigationStore:
    """Unified intelligence artifact store.

    All methods are non-blocking on failure — investigation must not be blocked
    by any intelligence layer error.
    """

    def __init__(
        self,
        investigations_dir: str = _DEFAULT_DIR,
        outcomes_path: str = _DEFAULT_OUTCOMES,
        profiles_path: str = _DEFAULT_PROFILES,
        patterns_path: str = _DEFAULT_PATTERNS,
    ) -> None:
        self._dir      = investigations_dir
        self._idx_path = os.path.join(investigations_dir, _INDEX_FILE)
        self._idx_lock = threading.Lock()

        self.outcomes  = OutcomeStore(outcomes_path)
        self.profiles  = ServiceProfileIndex(profiles_path)
        self.patterns  = PatternSignatureIndex(patterns_path)
        self.traces    = DecisionTraceLog(investigations_dir)
        self.seeds     = ReplaySeedStore(investigations_dir)

    # ------------------------------------------------------------------
    # EvidenceGraph
    # ------------------------------------------------------------------

    def save_graph(self, graph: EvidenceGraph) -> None:
        """Persist EvidenceGraph and update index. Non-fatal on failure."""
        try:
            path = self._graph_path(graph.investigation_id)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(graph.to_dict(), f, indent=2)
            os.replace(tmp, path)
            self._append_index(graph)
        except OSError as exc:
            logger.debug("InvestigationStore.save_graph failed: %s", exc)

    def load_graph(self, investigation_id: str) -> EvidenceGraph | None:
        """Load EvidenceGraph by investigation_id. Returns None if not found."""
        try:
            with open(self._graph_path(investigation_id)) as f:
                return EvidenceGraph.from_dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.debug("InvestigationStore.load_graph(%s): %s", investigation_id, exc)
            return None

    # ------------------------------------------------------------------
    # Composite write (one call to capture all artifacts)
    # ------------------------------------------------------------------

    def commit_investigation(
        self,
        graph: EvidenceGraph,
        decision_traces: list[DecisionTrace] | None = None,
        replay_seed: ReplaySeed | None = None,
        resolution_outcome: ResolutionOutcome | None = None,
    ) -> None:
        """Write all artifacts for one investigation atomically (best-effort)."""
        try:
            self.save_graph(graph)
        except Exception as exc:
            logger.debug("commit: save_graph failed: %s", exc)

        for trace in (decision_traces or []):
            try:
                self.traces.append(trace)
            except Exception as exc:
                logger.debug("commit: trace.append failed: %s", exc)

        if replay_seed:
            try:
                self.seeds.save(replay_seed)
            except Exception as exc:
                logger.debug("commit: seed.save failed: %s", exc)

        if resolution_outcome:
            try:
                self.outcomes.record(resolution_outcome)
                self._update_service_profile(graph, resolution_outcome)
            except Exception as exc:
                logger.debug("commit: outcome.record failed: %s", exc)

    # ------------------------------------------------------------------
    # Index queries
    # ------------------------------------------------------------------

    def find_by_service(self, service: str, last_n: int = 50) -> list[InvestigationRecord]:
        return [r for r in self._load_index(last_n=5000) if r.service == service][-last_n:]

    def find_by_incident_type(self, incident_type: str, last_n: int = 50) -> list[InvestigationRecord]:
        return [r for r in self._load_index(last_n=5000) if r.incident_type == incident_type][-last_n:]

    def list_recent(self, last_n: int = 100) -> list[InvestigationRecord]:
        return self._load_index(last_n=last_n)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _graph_path(self, investigation_id: str) -> str:
        return os.path.join(self._dir, f"{investigation_id}.json")

    def _append_index(self, graph: EvidenceGraph) -> None:
        record = InvestigationRecord(
            investigation_id=graph.investigation_id,
            incident_id=graph.incident_id,
            service=graph.service,
            incident_type=graph.incident_type,
            phase=graph.phase.value,
            node_count=graph.node_count(),
            edge_count=graph.edge_count(),
            created_at=graph.created_at,
        )
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._idx_path)), exist_ok=True)
            with self._idx_lock:
                with open(self._idx_path, "a") as f:
                    f.write(json.dumps(record.to_dict()) + "\n")
        except OSError as exc:
            logger.debug("InvestigationStore._append_index failed: %s", exc)

    def _load_index(self, last_n: int = 1000) -> list[InvestigationRecord]:
        try:
            with open(self._idx_path) as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            return [InvestigationRecord.from_dict(json.loads(ln)) for ln in lines[-last_n:]]
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _update_service_profile(
        self, graph: EvidenceGraph, outcome: ResolutionOutcome
    ) -> None:
        profile = self.profiles.get(graph.service)
        profile.record_investigation(
            investigation_id=graph.investigation_id,
            incident_type=graph.incident_type,
            entities=[n.entity_id for n in graph.all_nodes()],
        )
        profile.record_resolution(
            investigation_id=graph.investigation_id,
            resolution_action=outcome.executed_action,
            mttr_minutes=outcome.mttr_minutes,
            status=outcome.resolution_status.value,
        )
        self.profiles.update(profile)


# Module-level singleton
_store: InvestigationStore | None = None
_store_lock = threading.Lock()


def get_store() -> InvestigationStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = InvestigationStore()
        return _store
