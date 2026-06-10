"""Intel Writer — post-investigation coordinator.

Called from _post_flight_learning() in agent_harness.py after each investigation.
Non-blocking: every write is wrapped in try/except; failures are logged at DEBUG.

What it captures:
  - ResolutionMemory (candidate — pending human confirmation)
  - OperationalPattern (deterministic symptom grouping)
  - IncidentGraph nodes from root cause + service
  - DependencyGraph edges from evidence dependencies
  - ChangeImpactLinks from evidence change data
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.intelligence.intel_writer")

_DB_PATH = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")


def capture(
    investigation_id: str,
    incident_id: str,
    service: str,
    incident_type: str,
    result: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    environment: str = "",
    owner_team: str = "",
    mttr_minutes: float = 0.0,
) -> None:
    """Write all intel artifacts for one completed investigation. Non-blocking."""
    evidence = evidence or {}

    _capture_resolution_memory(
        investigation_id, incident_id, service, incident_type,
        result, evidence, environment, owner_team, mttr_minutes,
    )
    _capture_pattern(incident_type, result, service)
    _capture_incident_graph(investigation_id, incident_id, service, result, evidence)
    _capture_dependencies(service, evidence)
    _capture_change_impact(investigation_id, incident_id, service, evidence)


# ------------------------------------------------------------------
# Internal helpers — each wrapped to be non-fatal
# ------------------------------------------------------------------

def _capture_resolution_memory(
    investigation_id: str,
    incident_id: str,
    service: str,
    incident_type: str,
    result: dict[str, Any],
    evidence: dict[str, Any],
    environment: str,
    owner_team: str,
    mttr_minutes: float,
) -> None:
    try:
        from intelligence.resolution_memory import ResolutionMemory, ResolutionMemoryStore
        mem = ResolutionMemory.from_investigation(
            investigation_id=investigation_id,
            incident_id=incident_id,
            service=service,
            incident_type=incident_type,
            result=result,
            evidence=evidence,
            environment=environment,
            owner_team=owner_team,
            mttr_minutes=mttr_minutes,
        )
        store = ResolutionMemoryStore(_DB_PATH)
        store.record(mem)
        logger.debug("intel_writer: resolution memory recorded %s", mem.memory_id)
    except Exception as exc:
        logger.debug("intel_writer: resolution_memory failed: %s", exc)


def _capture_pattern(
    incident_type: str,
    result: dict[str, Any],
    service: str,
) -> None:
    try:
        from intelligence.pattern_intelligence import PatternIntelligenceStore
        root_cause = result.get("root_cause", "")
        if not root_cause:
            return
        quality = result.get("_online_quality_score", 0.0)
        resolved = quality >= 0.70
        store = PatternIntelligenceStore(_DB_PATH)
        pattern_id = store.record_occurrence(incident_type, root_cause, service, resolved)
        logger.debug("intel_writer: pattern recorded %s", pattern_id)
    except Exception as exc:
        logger.debug("intel_writer: pattern_intelligence failed: %s", exc)


def _capture_incident_graph(
    investigation_id: str,
    incident_id: str,
    service: str,
    result: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    try:
        from intelligence.incident_graph import IncidentGraphStore
        store = IncidentGraphStore(_DB_PATH)

        # Service node
        svc_node = store.make_node("service", service, incident_id, service=service)
        store.add_node(svc_node)

        # Root-cause node
        root_cause = result.get("root_cause", "")
        if root_cause:
            rc_node = store.make_node(
                "outcome", root_cause[:80], incident_id, service=service,
                properties={"confidence": result.get("confidence", 0)},
            )
            store.add_node(rc_node)
            edge = store.make_edge(
                svc_node.node_id, rc_node.node_id, "CAUSED_BY", incident_id,
            )
            store.add_edge(edge)

        # Alert nodes from evidence
        alerts = evidence.get("alerts", {})
        if isinstance(alerts, dict):
            for k in ("alerts_firing", "firing", "active"):
                for a in (alerts.get(k) or []):
                    if isinstance(a, str):
                        al_node = store.make_node("alert", a, incident_id, service=service)
                        store.add_node(al_node)
                        store.add_edge(
                            store.make_edge(al_node.node_id, svc_node.node_id, "AFFECTS", incident_id)
                        )

        logger.debug("intel_writer: incident graph updated for incident %s", incident_id)
    except Exception as exc:
        logger.debug("intel_writer: incident_graph failed: %s", exc)


def _capture_dependencies(service: str, evidence: dict[str, Any]) -> None:
    try:
        from intelligence.dependency_graph import DependencyGraphStore
        store = DependencyGraphStore(_DB_PATH)

        # Extract dependency hints from golden_signals or service_health evidence
        deps_raw = evidence.get("service_health", {})
        if isinstance(deps_raw, dict):
            for dep_svc in deps_raw.get("dependencies", []):
                if isinstance(dep_svc, str) and dep_svc != service:
                    store.record_dependency(service, dep_svc, dep_type="runtime")

        # Also check golden_signals upstream hints
        gs = evidence.get("golden_signals", {})
        if isinstance(gs, dict):
            for dep_svc in gs.get("upstream_services", []):
                if isinstance(dep_svc, str) and dep_svc != service:
                    store.record_dependency(service, dep_svc, dep_type="runtime")

        logger.debug("intel_writer: dependency graph updated for service %s", service)
    except Exception as exc:
        logger.debug("intel_writer: dependency_graph failed: %s", exc)


def _capture_change_impact(
    investigation_id: str,
    incident_id: str,
    service: str,
    evidence: dict[str, Any],
) -> None:
    try:
        from intelligence.change_tracker import ChangeImpactStore, score_change_impact
        store = ChangeImpactStore(_DB_PATH)

        changes_raw = evidence.get("change_data", {})
        if not isinstance(changes_raw, dict):
            return
        changes_list = changes_raw.get("changes", []) or changes_raw.get("recent_changes", [])

        incident_time = datetime.now(timezone.utc).isoformat()
        for ch in changes_list:
            if not isinstance(ch, dict):
                continue
            change = store.make_change(
                service=ch.get("service", service),
                change_type=ch.get("type", ch.get("change_type", "deployment")),
                deployed_at=ch.get("deployed_at", ch.get("timestamp", incident_time)),
                description=ch.get("description", ""),
                deployed_by=ch.get("deployed_by", ch.get("author", "")),
                metadata={k: v for k, v in ch.items()
                          if k not in {"service", "type", "change_type", "deployed_at", "description", "deployed_by"}},
            )
            store.record_change(change)
            score, reason = score_change_impact(change, service, incident_time)
            if score > 0:
                link = store.make_link(
                    change.change_id, incident_id, investigation_id, score, reason
                )
                store.link_to_incident(link)

        logger.debug("intel_writer: change impact captured for investigation %s", investigation_id)
    except Exception as exc:
        logger.debug("intel_writer: change_tracker failed: %s", exc)
