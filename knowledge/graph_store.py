"""High-level graph store for SentinalAI institutional knowledge.

Domain-level API over GraphBackendJson. Handles:
- Persisting completed investigations as graph nodes + edges
- Querying service history
- Confidence-gated persistence (blocked cases not stored by default)

Emits observability spans for all write operations.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from knowledge.graph_backend_json import GraphBackendJson
from supervisor.observability import trace_span

logger = logging.getLogger("sentinalai.knowledge.graph_store")

# Default persistence threshold — cases below this confidence are not stored
PERSISTENCE_CONFIDENCE_THRESHOLD = 30

# Default storage directory
DEFAULT_STORAGE_DIR = os.environ.get(
    "KNOWLEDGE_STORAGE_DIR",
    os.path.join(os.path.dirname(__file__), ".knowledge_store"),
)


class GraphStore:
    """Domain-level graph store for institutional incident knowledge."""

    def __init__(self, storage_dir: str | None = None):
        self.backend = GraphBackendJson(storage_dir or DEFAULT_STORAGE_DIR)

    def persist_investigation(
        self,
        incident_id: str,
        incident_type: str,
        service: str,
        root_cause: str,
        confidence: int,
        evidence_refs: list[str],
        environment: str = "",
    ) -> bool:
        """Persist a completed investigation as graph nodes and edges.

        Does NOT persist blocked cases (confidence < threshold).

        Creates:
            - incident node
            - service node
            - causal_artifact node
            - Edges: incident->service (affects), incident->artifact (proven_by)

        Returns:
            True if persisted, False if skipped.
        """
        if confidence < PERSISTENCE_CONFIDENCE_THRESHOLD:
            logger.debug(
                "Skipping persistence for %s (confidence=%d < %d)",
                incident_id, confidence, PERSISTENCE_CONFIDENCE_THRESHOLD,
            )
            return False

        with trace_span("graph_upsert", case_id=incident_id) as span:
            span.set_attribute("incident_type", incident_type)
            span.set_attribute("service", service)

            # Incident node
            self.backend.upsert_node("incident", incident_id, {
                "incident_type": incident_type,
                "service": service,
                "root_cause": root_cause,
                "confidence": confidence,
                "environment": environment,
                "evidence_refs": evidence_refs,
            })

            # Service node
            self.backend.upsert_node("service", service, {
                "service": service,
            })

            # Causal artifact node
            artifact_id = f"CA-{incident_id}"
            self.backend.upsert_node("causal_artifact", artifact_id, {
                "root_cause": root_cause,
                "confidence": confidence,
                "incident_type": incident_type,
            })

            # Edges
            self.backend.add_edge(incident_id, "affects_service", service)
            self.backend.add_edge(incident_id, "proven_by", artifact_id, weight=confidence / 100.0)

            span.set_attribute("nodes_written", 3)
            span.set_attribute("edges_written", 2)

        return True

    def get_service_history(
        self,
        service: str,
    ) -> list[dict[str, Any]]:
        """Get all historical incidents for a service."""
        nodes = self.backend.get_nodes(
            node_type="incident",
            metadata_filter={"service": service},
        )
        return nodes
