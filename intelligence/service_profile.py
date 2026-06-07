"""ServiceProfile — per-service long-term operational learning.

Aggregates: recurring failures, entities, dependencies, symptoms, resolutions,
investigation history, failure history, and MTTR statistics.

Updated by InvestigationStore after each investigation completes.
Persisted to eval/service_profiles.json (full replace, atomic write).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.service_profile")

_DEFAULT_PATH = os.getenv("SERVICE_PROFILES_PATH", "eval/service_profiles.json")
_MAX_HISTORY  = int(os.getenv("SERVICE_PROFILE_MAX_HISTORY", "200"))


@dataclass
class ServiceProfile:
    profile_id:              str         # = service_name (stable key)
    service_name:            str
    service_id:              str         # future CMDB GUID
    application_id:          str         # future CMDB GUID
    recurring_incident_types: dict[str, int]  = field(default_factory=dict)
    recurring_entities:       dict[str, int]  = field(default_factory=dict)
    recurring_dependencies:   dict[str, int]  = field(default_factory=dict)
    recurring_symptoms:       dict[str, int]  = field(default_factory=dict)
    recurring_resolutions:    dict[str, int]  = field(default_factory=dict)
    investigation_ids:        list[str]       = field(default_factory=list)
    failure_history:          list[dict]      = field(default_factory=list)
    resolution_history:       list[dict]      = field(default_factory=list)
    total_investigations:     int = 0
    avg_mttr_minutes:         float = 0.0
    last_updated:             str = ""
    first_seen:               str = ""

    # ------------------------------------------------------------------

    def record_investigation(
        self,
        investigation_id: str,
        incident_type: str,
        entities: list[str] | None = None,
        dependencies: list[str] | None = None,
        symptoms: list[str] | None = None,
    ) -> None:
        if investigation_id not in self.investigation_ids:
            self.investigation_ids.append(investigation_id)
        self.total_investigations += 1
        self.recurring_incident_types[incident_type] = (
            self.recurring_incident_types.get(incident_type, 0) + 1
        )
        for e in (entities or []):
            self.recurring_entities[e] = self.recurring_entities.get(e, 0) + 1
        for d in (dependencies or []):
            self.recurring_dependencies[d] = self.recurring_dependencies.get(d, 0) + 1
        for s in (symptoms or []):
            self.recurring_symptoms[s] = self.recurring_symptoms.get(s, 0) + 1
        self.last_updated = datetime.now(timezone.utc).isoformat()
        if not self.first_seen:
            self.first_seen = self.last_updated
        # Keep lists bounded
        if len(self.investigation_ids) > _MAX_HISTORY:
            self.investigation_ids = self.investigation_ids[-_MAX_HISTORY:]

    def record_resolution(
        self,
        investigation_id: str,
        resolution_action: str,
        mttr_minutes: float,
        status: str,
    ) -> None:
        self.recurring_resolutions[resolution_action] = (
            self.recurring_resolutions.get(resolution_action, 0) + 1
        )
        entry = {
            "investigation_id": investigation_id,
            "action": resolution_action,
            "mttr_minutes": mttr_minutes,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.resolution_history.append(entry)
        self.failure_history.append({
            "investigation_id": investigation_id,
            "mttr_minutes": mttr_minutes,
            "status": status,
            "timestamp": entry["timestamp"],
        })
        # Update rolling MTTR average
        recent = [r["mttr_minutes"] for r in self.resolution_history[-20:]]
        self.avg_mttr_minutes = round(sum(recent) / len(recent), 1) if recent else 0.0
        # Bound history
        if len(self.resolution_history) > _MAX_HISTORY:
            self.resolution_history = self.resolution_history[-_MAX_HISTORY:]
        if len(self.failure_history) > _MAX_HISTORY:
            self.failure_history = self.failure_history[-_MAX_HISTORY:]
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def top_incident_types(self, n: int = 3) -> list[str]:
        return sorted(self.recurring_incident_types, key=lambda k: -self.recurring_incident_types[k])[:n]

    def top_resolutions(self, n: int = 3) -> list[str]:
        return sorted(self.recurring_resolutions, key=lambda k: -self.recurring_resolutions[k])[:n]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id":              self.profile_id,
            "service_name":            self.service_name,
            "service_id":              self.service_id,
            "application_id":          self.application_id,
            "recurring_incident_types": self.recurring_incident_types,
            "recurring_entities":       self.recurring_entities,
            "recurring_dependencies":   self.recurring_dependencies,
            "recurring_symptoms":       self.recurring_symptoms,
            "recurring_resolutions":    self.recurring_resolutions,
            "investigation_ids":        self.investigation_ids,
            "failure_history":          self.failure_history,
            "resolution_history":       self.resolution_history,
            "total_investigations":     self.total_investigations,
            "avg_mttr_minutes":         self.avg_mttr_minutes,
            "last_updated":             self.last_updated,
            "first_seen":               self.first_seen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ServiceProfile":
        return cls(
            profile_id=d["profile_id"],
            service_name=d["service_name"],
            service_id=d.get("service_id", ""),
            application_id=d.get("application_id", ""),
            recurring_incident_types=d.get("recurring_incident_types", {}),
            recurring_entities=d.get("recurring_entities", {}),
            recurring_dependencies=d.get("recurring_dependencies", {}),
            recurring_symptoms=d.get("recurring_symptoms", {}),
            recurring_resolutions=d.get("recurring_resolutions", {}),
            investigation_ids=d.get("investigation_ids", []),
            failure_history=d.get("failure_history", []),
            resolution_history=d.get("resolution_history", []),
            total_investigations=d.get("total_investigations", 0),
            avg_mttr_minutes=float(d.get("avg_mttr_minutes", 0.0)),
            last_updated=d.get("last_updated", ""),
            first_seen=d.get("first_seen", ""),
        )

    @classmethod
    def new(cls, service_name: str) -> "ServiceProfile":
        return cls(
            profile_id=service_name,
            service_name=service_name,
            service_id="",
            application_id="",
        )


class ServiceProfileIndex:
    """Persistent index of all service profiles. Thread-safe."""

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._profiles: dict[str, ServiceProfile] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._profiles = {k: ServiceProfile.from_dict(v) for k, v in data.items()}
        except (FileNotFoundError, json.JSONDecodeError):
            self._profiles = {}
        self._loaded = True

    def get(self, service_name: str) -> ServiceProfile:
        with self._lock:
            self._ensure_loaded()
            if service_name not in self._profiles:
                self._profiles[service_name] = ServiceProfile.new(service_name)
            return self._profiles[service_name]

    def update(self, profile: ServiceProfile) -> None:
        with self._lock:
            self._ensure_loaded()
            self._profiles[profile.service_name] = profile
            self._flush_locked()

    def all_services(self) -> list[str]:
        with self._lock:
            self._ensure_loaded()
            return list(self._profiles.keys())

    def _flush_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
            tmp = self._path + ".tmp"
            data = {k: v.to_dict() for k, v in self._profiles.items()}
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.debug("ServiceProfileIndex flush failed: %s", exc)
