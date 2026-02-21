"""Knowledge Worker - handles historical incident context.

Uses AgentCore Memory (long-term memory) for semantic similarity search
against past investigations. Falls back gracefully when memory is not
configured — returns empty results (no errors).

For testing, the execute() method is monkey-patched with mocks.
"""

from __future__ import annotations

import logging

from workers.base_worker import BaseWorker

logger = logging.getLogger("sentinalai.knowledge_worker")


class KnowledgeWorker(BaseWorker):
    """Worker that searches historical incident knowledge base."""

    worker_name = "knowledge_worker"

    def __init__(self):
        super().__init__()
        self.register("search_similar", self._search_similar)
        self.register("store_result", self._store_result)

    def _search_similar(self, params: dict) -> dict:
        """Search for historically similar incidents via AgentCore Memory.

        Params:
            service: Service name to filter by
            summary: Natural language description of the incident

        Returns:
            {"similar_incidents": [...]} — empty list if memory unavailable
        """
        try:
            from supervisor.memory import search_similar_incidents, is_enabled

            if not is_enabled():
                return {"similar_incidents": []}

            service = params.get("service", "")
            summary = params.get("summary", "")

            if not service and not summary:
                return {"similar_incidents": []}

            results = search_similar_incidents(
                service=service,
                query=summary or service,
            )

            return {"similar_incidents": results}

        except ImportError:
            return {"similar_incidents": []}
        except Exception as exc:
            logger.warning("Knowledge search failed: %s", exc)
            return {"similar_incidents": []}

    def _store_result(self, params: dict) -> dict:
        """Store a completed investigation result in long-term memory.

        Params:
            incident_id, incident_type, service, root_cause,
            confidence, reasoning, evidence_summary (optional)

        Returns:
            {"stored": True/False}
        """
        try:
            from supervisor.memory import store_investigation_result, is_enabled

            if not is_enabled():
                return {"stored": False, "reason": "memory_not_configured"}

            stored = store_investigation_result(
                incident_id=params.get("incident_id", ""),
                incident_type=params.get("incident_type", ""),
                service=params.get("service", ""),
                root_cause=params.get("root_cause", ""),
                confidence=params.get("confidence", 0),
                reasoning=params.get("reasoning", ""),
                evidence_summary=params.get("evidence_summary", ""),
            )
            return {"stored": stored}

        except ImportError:
            return {"stored": False, "reason": "memory_sdk_unavailable"}
        except Exception as exc:
            logger.warning("Knowledge store failed: %s", exc)
            return {"stored": False, "reason": str(exc)}
