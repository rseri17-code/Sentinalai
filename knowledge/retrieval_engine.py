"""Retrieval engine for institutional knowledge similarity search.

Performs metadata-filtered, structured similarity retrieval against
the graph store. Returns structured matches only — the LLM never
sees raw historical documents.

Retrieval is proof-gated: results are used only to suggest next
detectors or provide a bounded confidence boost. Retrieval cannot
produce RCA or override the deterministic proof rule.

Retrieval pipeline (as of Phase 3 upgrade):
  1. Metadata filter  — hard filter on service/env/time
  2. Hybrid ranking   — BM25 + TF-IDF cosine + source confidence
  3. Reranking        — cross-field signals (service, type, window, staleness)
  4. Cache            — TTL cache keyed on (service, incident_type, query)
  5. Telemetry        — structured NDJSON log per retrieval event
"""

from __future__ import annotations

import logging
import time
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

            # Step 3: Check retrieval cache
            t0 = time.monotonic()
            cache_hit = False
            cache_key: str | None = None
            try:
                from supervisor.retrieval.retrieval_cache import get_cache
                _cache = get_cache()
                cached = _cache.get(service, incident_type, summary)
                if cached is not None:
                    span.set_attribute("cache_hit", True)
                    span.set_attribute("retrieval_count", len(cached))
                    return [_cache_entry_to_match(c) for c in cached]
            except Exception as exc:
                logger.debug("Retrieval cache check failed (non-critical): %s", exc)

            # Step 4: Hybrid rank — BM25 + TF-IDF cosine + source confidence
            try:
                from supervisor.retrieval.hybrid_retriever import rank as _hybrid_rank
                from supervisor.retrieval.reranker import rerank as _rerank

                hybrid_candidates = [
                    {
                        "doc_id": node["node_id"],
                        "text": " ".join([
                            node.get("metadata", {}).get("root_cause", ""),
                            node.get("metadata", {}).get("incident_type", ""),
                            node.get("metadata", {}).get("service", ""),
                        ]),
                        "source_type": "experience_store",
                        "collected_at": node.get("metadata", {}).get("timestamp"),
                        "metadata": node.get("metadata", {}),
                    }
                    for node in filtered
                ]

                query_text = f"{incident_type} {summary}"
                ranked = _hybrid_rank(query_text, hybrid_candidates, top_k=top_k * 2)
                reranked = _rerank(ranked, service=service, incident_type=incident_type)
                top = reranked[:top_k]

                results = [
                    {
                        "incident_id": r.doc_id,
                        "root_cause": r.metadata.get("root_cause", ""),
                        "similarity_score": round(min(1.0, r.rerank_score), 4),
                        "incident_type": r.metadata.get("incident_type", ""),
                        "source_confidence": r.hybrid_score,
                        "is_stale": r.is_stale,
                    }
                    for r in top
                ]
            except Exception as exc:
                # Fallback to legacy Jaccard scoring if hybrid pipeline fails
                logger.debug("Hybrid retrieval failed, using legacy scoring: %s", exc)
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
                scored.sort(key=lambda x: -x["similarity_score"])
                results = scored[:top_k]
                top = []  # no reranked candidates for telemetry

            latency_ms = (time.monotonic() - t0) * 1000
            span.set_attribute("retrieval_count", len(results))
            if results:
                span.set_attribute("top_similarity", results[0]["similarity_score"])

            # Step 5: Store in cache
            try:
                from supervisor.retrieval.retrieval_cache import get_cache
                cache_key = get_cache().put(service, incident_type, summary, results)
            except Exception as exc:
                logger.debug("Retrieval cache store failed (non-critical): %s", exc)

            # Step 6: Telemetry
            try:
                from supervisor.retrieval.telemetry import log_retrieval_event
                if top:
                    log_retrieval_event(
                        incident_id=service,
                        service=service,
                        incident_type=incident_type,
                        query=summary,
                        candidates_in=len(filtered),
                        results=top,
                        cache_hit=cache_hit,
                        cache_key=cache_key,
                        latency_ms=latency_ms,
                    )
            except Exception as exc:
                logger.debug("Retrieval telemetry failed (non-critical): %s", exc)

            return results


def _cache_entry_to_match(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert a cached result dict back to a retrieve_similar match dict."""
    return {
        "incident_id": entry.get("doc_id", entry.get("incident_id", "")),
        "root_cause": entry.get("metadata", {}).get("root_cause", entry.get("root_cause", "")),
        "similarity_score": entry.get("score", entry.get("similarity_score", 0.0)),
        "incident_type": entry.get("metadata", {}).get("incident_type", entry.get("incident_type", "")),
    }


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
