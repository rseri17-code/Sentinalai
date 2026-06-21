"""Topology Learner — auto-discovers service relationships from investigation evidence.

Extracts service identifiers from evidence dicts produced by investigations and
updates the CausalGraph so the topology improves over time without manual seeding.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from intelligence.causal_graph import CausalGraph

logger = logging.getLogger("sentinalai.topology_learner")

# Regex: snake_case or kebab-case tokens that look like service identifiers
_SERVICE_PATTERN = re.compile(
    r"\b([a-z][a-z0-9]*(?:[-_][a-z0-9]+)*)\b"
)

# Keywords that suggest a token is a service name
_SERVICE_KEYWORDS = frozenset(
    ["service", "worker", "api", "db", "cache", "queue", "broker", "engine", "gateway"]
)

# Evidence fields that may contain co-failing service references
_CO_FAILURE_FIELDS = (
    "blast_radius",
    "cmdb_blast_radius",
    "itsm_context",
    "evidence_timeline",
    "apm_traces",
    "dependency_graph",
    "affected_services",
)

# Evidence fields that may carry health data
_HEALTH_FIELDS = ("health_score", "error_rate", "slo_status")


def _looks_like_service(token: str) -> bool:
    """Return True if the token matches known service-name patterns."""
    lower = token.lower()
    return any(kw in lower for kw in _SERVICE_KEYWORDS)


def _extract_service_ids(value) -> list[str]:
    """Recursively extract service-like strings from a value (str/list/dict)."""
    found: list[str] = []
    if isinstance(value, str):
        for match in _SERVICE_PATTERN.finditer(value):
            token = match.group(1)
            if _looks_like_service(token):
                found.append(token)
    elif isinstance(value, dict):
        # Check common keys that hold a service id directly
        for id_key in ("service_id", "ci_name", "service", "name"):
            candidate = value.get(id_key)
            if isinstance(candidate, str) and candidate:
                found.append(candidate)
        # Recurse into all values
        for v in value.values():
            found.extend(_extract_service_ids(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_extract_service_ids(item))
    return found


def _parse_health(evidence: dict) -> Optional[tuple[float, int]]:
    """Return (health_float, alert_count) from evidence, or None if not present."""
    try:
        health: Optional[float] = None
        alert_count = 0

        if "health_score" in evidence:
            raw = evidence["health_score"]
            health = max(0.0, min(1.0, float(raw)))

        elif "error_rate" in evidence:
            raw = evidence["error_rate"]
            rate = float(raw)
            # error_rate in [0,1] → health = 1 - rate
            # error_rate in (1, 100] → treat as percentage
            if rate > 1.0:
                rate = rate / 100.0
            health = max(0.0, min(1.0, 1.0 - rate))

        elif "slo_status" in evidence:
            status = str(evidence["slo_status"]).lower()
            if "breach" in status or "violation" in status or "error" in status:
                health = 0.4
            elif "warning" in status or "at_risk" in status:
                health = 0.7
            else:
                health = 0.95

        if health is None:
            return None

        # Best-effort alert count
        if "alert_count" in evidence:
            alert_count = int(evidence["alert_count"])
        elif "alerts" in evidence and isinstance(evidence["alerts"], list):
            alert_count = len(evidence["alerts"])

        return health, alert_count
    except Exception:
        return None


class TopologyLearner:
    """Extracts service relationships from investigation evidence and updates CausalGraph."""

    def __init__(self, graph: CausalGraph) -> None:
        self._graph = graph

    def learn_from_evidence(
        self,
        primary_service: str,
        incident_type: str,
        evidence: dict,
        elapsed_ms: int = 0,
    ) -> int:
        """Parse evidence and update the causal graph.

        Returns the number of graph updates (edge creations/updates + health updates).
        Never raises — all errors are logged at debug level.
        """
        try:
            if not evidence or not isinstance(evidence, dict):
                return 0

            updates = 0

            # --- Health update for primary service ---
            health_data = _parse_health(evidence)
            if health_data is not None:
                health, alert_count = health_data
                self._graph.update_service_health(primary_service, health, alert_count)
                updates += 1

            # --- Co-failure edge discovery ---
            discovered: set[str] = set()

            for field in _CO_FAILURE_FIELDS:
                if field not in evidence:
                    continue
                value = evidence[field]
                for svc_id in _extract_service_ids(value):
                    if svc_id and svc_id != primary_service:
                        discovered.add(svc_id)

            for other_service in discovered:
                self._graph.record_co_failure(primary_service, other_service, elapsed_ms)
                updates += 1

            return updates
        except Exception as exc:
            logger.debug(
                "TopologyLearner.learn_from_evidence failed (non-critical): %s", exc
            )
            return 0


# ---------------------------------------------------------------------------
# Module-level singleton + convenience function
# ---------------------------------------------------------------------------

_singleton_graph: Optional[CausalGraph] = None
_singleton_learner: Optional[TopologyLearner] = None


def _get_learner() -> TopologyLearner:
    global _singleton_graph, _singleton_learner
    if _singleton_learner is None:
        _singleton_graph = CausalGraph()
        _singleton_learner = TopologyLearner(_singleton_graph)
    return _singleton_learner


def learn(
    primary_service: str,
    incident_type: str,
    evidence: dict,
    elapsed_ms: int = 0,
) -> int:
    """Convenience wrapper using the module-level CausalGraph singleton."""
    try:
        return _get_learner().learn_from_evidence(
            primary_service, incident_type, evidence, elapsed_ms
        )
    except Exception as exc:
        logger.debug("topology_learner.learn failed (non-critical): %s", exc)
        return 0
