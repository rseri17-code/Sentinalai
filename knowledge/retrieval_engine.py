"""Retrieval engine for institutional knowledge similarity search.

Performs metadata-filtered, structured similarity retrieval against
the graph store. Returns structured matches only — the LLM never
sees raw historical documents.

Retrieval is proof-gated: results are used only to suggest next
detectors or provide a bounded confidence boost. Retrieval cannot
produce RCA or override the deterministic proof rule.
"""

from __future__ import annotations

import logging
from typing import Any

from knowledge.graph_store import GraphStore
from knowledge.metadata_filter import filter_by_metadata
from supervisor.observability import trace_span

logger = logging.getLogger("sentinalai.knowledge.retrieval")

# Maximum confidence boost from retrieval (absolute cap)
MAX_RETRIEVAL_BOOST = 10


class RetrievalEngine:
    """Structured similarity retrieval over institutional knowledge."""

    def __init__(self, graph_store: GraphStore):
        self._store = graph_store

    def retrieve_similar(
        self,
        service: str,
        incident_type: str,
        summary: str,
        top_k: int = 3,
        environment: str | None = None,
        time_window_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve similar historical incidents.

        Process:
            1. Build incident signature from service + incident_type + summary
            2. Apply metadata filter (hard filter — empty = skip)
            3. Score by token overlap similarity
            4. Return structured matches only

        Args:
            service: Service to filter by
            incident_type: Current incident classification
            summary: Natural language incident summary
            top_k: Max results to return
            environment: Optional environment filter
            time_window_seconds: Optional time window filter

        Returns:
            List of structured match dicts with:
                incident_id, root_cause, similarity_score, incident_type
        """
        with trace_span("retrieval_execution", case_id=service) as span:
            span.set_attribute("service", service)
            span.set_attribute("incident_type", incident_type)

            # Step 1: Get candidate nodes from graph
            candidates = self._store.backend.get_nodes(node_type="incident")

            if not candidates:
                span.set_attribute("retrieval_count", 0)
                return []

            # Step 2: Hard metadata filter (mandatory)
            filtered = filter_by_metadata(
                candidates,
                service=service,
                environment=environment,
                time_window_seconds=time_window_seconds,
            )

            if not filtered:
                span.set_attribute("retrieval_count", 0)
                return []

            # Step 3: Score by similarity
            scored = []
            for node in filtered:
                meta = node.get("metadata", {})
                score = _compute_similarity(
                    query_type=incident_type,
                    query_summary=summary,
                    candidate_type=meta.get("incident_type", ""),
                    candidate_root_cause=meta.get("root_cause", ""),
                )
                scored.append({
                    "incident_id": node["node_id"],
                    "root_cause": meta.get("root_cause", ""),
                    "similarity_score": score,
                    "incident_type": meta.get("incident_type", ""),
                })

            # Step 4: Sort and truncate
            scored.sort(key=lambda x: -x["similarity_score"])
            results = scored[:top_k]

            span.set_attribute("retrieval_count", len(results))
            if results:
                span.set_attribute("top_similarity", results[0]["similarity_score"])

            return results


def _compute_similarity(
    query_type: str,
    query_summary: str,
    candidate_type: str,
    candidate_root_cause: str,
) -> float:
    """Compute similarity score between query and candidate.

    Uses token overlap for simplicity. Incident type match gives a
    significant boost. Score bounded to [0.0, 1.0].
    """
    score = 0.0

    # Type match bonus (strongest signal)
    if query_type and candidate_type and query_type == candidate_type:
        score += 0.5

    # Token overlap between summary and root cause
    query_tokens = set(query_summary.lower().split())
    candidate_tokens = set(candidate_root_cause.lower().split())

    if query_tokens and candidate_tokens:
        intersection = query_tokens & candidate_tokens
        union = query_tokens | candidate_tokens
        jaccard = len(intersection) / len(union) if union else 0.0
        score += jaccard * 0.5

    return min(1.0, max(0.0, round(score, 4)))


def compute_retrieval_boost(
    matches: list[dict[str, Any]],
) -> float:
    """Compute confidence boost from retrieval results.

    Constraints:
        - Capped at MAX_RETRIEVAL_BOOST (10)
        - Proportional to best similarity score
        - Zero if no matches

    Returns:
        Float boost value in [0, MAX_RETRIEVAL_BOOST]
    """
    if not matches:
        return 0.0

    best_score = max(m.get("similarity_score", 0.0) for m in matches)
    boost = best_score * MAX_RETRIEVAL_BOOST

    return min(MAX_RETRIEVAL_BOOST, round(boost, 1))
