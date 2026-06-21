"""Episodic Memory — stores compressed investigation episodes for cross-investigation retrieval.

An episode is a compressed summary of one investigation: what was observed,
what was concluded, and what was done to resolve it.

Storage: JSONL (append-only) at eval/episodic_memory.jsonl
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from intelligence.semantic_search import SemanticIndex

logger = logging.getLogger("sentinalai.intelligence.episodic_memory")

_DEFAULT_STORAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "eval", "episodic_memory.jsonl"
)


@dataclass
class Episode:
    episode_id: str            # uuid
    incident_id: str
    service: str
    incident_type: str         # "timeout" | "latency" | etc.
    failure_signature: str     # short text descriptor
    root_cause: str
    confidence: float          # 0.0-1.0
    resolution_action: str     # "restarted connection pool", etc.
    resolved_by: str           # "auto" | "SRE-on-call" | "unknown"
    time_to_resolve_ms: int    # total resolution time
    evidence_keys: list        # which evidence sources were most informative
    outcome: str               # "resolved" | "escalated" | "auto-remediated" | "unknown"
    tags: list                 # ["database", "connection-pool", "peak-traffic"]
    recorded_at: str           # ISO timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        return cls(
            episode_id=d.get("episode_id", str(uuid.uuid4())),
            incident_id=d.get("incident_id", ""),
            service=d.get("service", ""),
            incident_type=d.get("incident_type", ""),
            failure_signature=d.get("failure_signature", ""),
            root_cause=d.get("root_cause", ""),
            confidence=float(d.get("confidence", 0.0)),
            resolution_action=d.get("resolution_action", ""),
            resolved_by=d.get("resolved_by", "unknown"),
            time_to_resolve_ms=int(d.get("time_to_resolve_ms", 0)),
            evidence_keys=list(d.get("evidence_keys", [])),
            outcome=d.get("outcome", "unknown"),
            tags=list(d.get("tags", [])),
            recorded_at=d.get("recorded_at", datetime.now(timezone.utc).isoformat()),
        )


class EpisodicMemory:
    """JSONL-backed episodic memory for past incident investigations."""

    def __init__(self, storage_path: str = _DEFAULT_STORAGE_PATH) -> None:
        self._path = storage_path
        self._episodes: list = []
        self._index: Optional[SemanticIndex] = None
        self._load()
        if not self._episodes:
            self.seed_demo_episodes()

    def _load(self) -> None:
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._episodes.append(Episode.from_dict(json.loads(line)))
                        except Exception as exc:
                            logger.debug("Skipping malformed episode line: %s", exc)
        except Exception as exc:
            logger.warning("EpisodicMemory load failed: %s", exc)

    def _build_index(self) -> SemanticIndex:
        idx = SemanticIndex()
        for ep in self._episodes:
            idx.add(ep.episode_id, ep.failure_signature)
        self._index = idx
        return idx

    def record(self, episode: Episode) -> None:
        """Append an episode to the JSONL store."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(episode.to_dict()) + "\n")
            self._episodes.append(episode)
            self._index = None  # invalidate so next search rebuilds
        except Exception as exc:
            logger.warning("EpisodicMemory.record failed: %s", exc)

    def search(
        self,
        service: Optional[str] = None,
        incident_type: Optional[str] = None,
        failure_signature: Optional[str] = None,
        limit: int = 5,
    ) -> list:
        """Filter episodes by any combination of fields.

        If failure_signature is provided, score by TF-IDF cosine similarity
        and return top-limit results.
        """
        try:
            candidates = list(self._episodes)

            if service:
                candidates = [e for e in candidates if e.service == service]
            if incident_type:
                candidates = [e for e in candidates if e.incident_type == incident_type]

            if failure_signature:
                if not candidates:
                    return []
                idx = SemanticIndex()
                for ep in candidates:
                    idx.add(ep.episode_id, ep.failure_signature)
                ep_by_id = {ep.episode_id: ep for ep in candidates}
                hits = idx.search(failure_signature, top_k=limit)
                return [ep_by_id[id] for id, _ in hits if id in ep_by_id]

            return candidates[:limit]
        except Exception as exc:
            logger.warning("EpisodicMemory.search failed: %s", exc)
            return []

    def get_similar(
        self,
        failure_signature: str,
        service: Optional[str] = None,
        limit: int = 3,
    ) -> list:
        """Return episodes most similar to the given failure signature."""
        try:
            if service:
                candidates = [e for e in self._episodes if e.service == service]
                idx = SemanticIndex()
                for ep in candidates:
                    idx.add(ep.episode_id, ep.failure_signature)
                ep_by_id = {ep.episode_id: ep for ep in candidates}
            else:
                if self._index is None:
                    self._build_index()
                idx = self._index
                ep_by_id = {ep.episode_id: ep for ep in self._episodes}

            hits = idx.search(failure_signature, top_k=limit)
            return [ep_by_id[id] for id, score in hits if score > 0 and id in ep_by_id]
        except Exception as exc:
            logger.warning("EpisodicMemory.get_similar failed: %s", exc)
            return []

    def get_resolution_success_rate(
        self, incident_type: str, resolution_action: str
    ) -> float:
        """Return fraction of times this action resolved this incident type (0.0-1.0)."""
        try:
            matching = [
                e for e in self._episodes
                if e.incident_type == incident_type
                and e.resolution_action == resolution_action
            ]
            if not matching:
                return 0.0
            resolved = [
                e for e in matching
                if e.outcome in ("resolved", "auto-remediated")
            ]
            return len(resolved) / len(matching)
        except Exception as exc:
            logger.warning("EpisodicMemory.get_resolution_success_rate failed: %s", exc)
            return 0.0

    def summary_for_service(self, service: str) -> dict:
        """Return aggregated stats for a service."""
        try:
            episodes = [e for e in self._episodes if e.service == service]
            if not episodes:
                return {
                    "total_incidents": 0,
                    "avg_ttresolve_ms": 0,
                    "most_common_type": "",
                    "most_successful_action": "",
                }

            avg_ttresolve = int(
                sum(e.time_to_resolve_ms for e in episodes) / len(episodes)
            )

            type_counts: dict = {}
            for e in episodes:
                type_counts[e.incident_type] = type_counts.get(e.incident_type, 0) + 1
            most_common_type = max(type_counts, key=lambda k: type_counts[k]) if type_counts else ""

            action_success: dict = {}
            action_total: dict = {}
            for e in episodes:
                action_total[e.resolution_action] = action_total.get(e.resolution_action, 0) + 1
                if e.outcome in ("resolved", "auto-remediated"):
                    action_success[e.resolution_action] = action_success.get(e.resolution_action, 0) + 1
            best_action = ""
            best_rate = -1.0
            for action, total in action_total.items():
                rate = action_success.get(action, 0) / total
                if rate > best_rate:
                    best_rate = rate
                    best_action = action

            return {
                "total_incidents": len(episodes),
                "avg_ttresolve_ms": avg_ttresolve,
                "most_common_type": most_common_type,
                "most_successful_action": best_action,
            }
        except Exception as exc:
            logger.warning("EpisodicMemory.summary_for_service failed: %s", exc)
            return {
                "total_incidents": 0,
                "avg_ttresolve_ms": 0,
                "most_common_type": "",
                "most_successful_action": "",
            }

    def seed_demo_episodes(self) -> None:
        """Seed 20 realistic past episodes if the store is empty."""
        try:
            if os.path.exists(self._path):
                return

            now_base = "2026-06-01T00:00:00+00:00"

            demos = [
                # 3x payment-service / timeout — connection pool exhausted
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2001",
                    service="payment-service",
                    incident_type="timeout",
                    failure_signature="postgres connection pool exhausted",
                    root_cause="All 20 Postgres connection pool slots occupied; new queries time out after 30s",
                    confidence=0.91,
                    resolution_action="increase pool size",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=480_000,
                    evidence_keys=["search_logs", "get_golden_signals", "query_metrics"],
                    outcome="resolved",
                    tags=["database", "connection-pool", "postgres"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2002",
                    service="payment-service",
                    incident_type="timeout",
                    failure_signature="postgres connection pool exhausted peak traffic",
                    root_cause="Connection pool exhausted during Black Friday peak; pool size 20 insufficient",
                    confidence=0.88,
                    resolution_action="increase pool size",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=510_000,
                    evidence_keys=["search_logs", "get_golden_signals", "itsm_context"],
                    outcome="resolved",
                    tags=["database", "connection-pool", "peak-traffic"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2003",
                    service="payment-service",
                    incident_type="timeout",
                    failure_signature="postgres connection pool exhausted high load",
                    root_cause="DB connection pool saturated; upstream retries amplified load",
                    confidence=0.85,
                    resolution_action="increase pool size",
                    resolved_by="auto",
                    time_to_resolve_ms=460_000,
                    evidence_keys=["search_logs", "query_metrics", "cmdb_blast_radius"],
                    outcome="auto-remediated",
                    tags=["database", "connection-pool", "high-load"],
                    recorded_at=now_base,
                ),
                # 2x payment-service / latency — slow postgres query
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2004",
                    service="payment-service",
                    incident_type="latency",
                    failure_signature="slow postgres query sequential scan orders table",
                    root_cause="Missing index on orders.created_at causing full table sequential scan",
                    confidence=0.93,
                    resolution_action="add index on orders.created_at",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=1_500_000,
                    evidence_keys=["search_logs", "get_golden_signals", "diff_analysis"],
                    outcome="resolved",
                    tags=["database", "query-performance", "missing-index"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2005",
                    service="payment-service",
                    incident_type="latency",
                    failure_signature="slow postgres query orders.created_at no index",
                    root_cause="New query pattern introduced in v3.2 uses orders.created_at without index",
                    confidence=0.89,
                    resolution_action="add index on orders.created_at",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=1_620_000,
                    evidence_keys=["search_logs", "diff_analysis", "devops_context"],
                    outcome="resolved",
                    tags=["database", "query-performance", "missing-index", "deployment"],
                    recorded_at=now_base,
                ),
                # 3x cart-service / error_spike — redis eviction
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2006",
                    service="cart-service",
                    incident_type="error_spike",
                    failure_signature="redis eviction under load maxmemory exceeded",
                    root_cause="Redis maxmemory limit reached; LRU evicting active cart sessions causing 503s",
                    confidence=0.92,
                    resolution_action="increase redis maxmemory",
                    resolved_by="auto",
                    time_to_resolve_ms=300_000,
                    evidence_keys=["search_logs", "get_golden_signals", "query_metrics"],
                    outcome="auto-remediated",
                    tags=["redis", "cache-eviction", "memory"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2007",
                    service="cart-service",
                    incident_type="error_spike",
                    failure_signature="redis eviction cart sessions lost",
                    root_cause="Redis evicting cart sessions; maxmemory 512MB insufficient during sale event",
                    confidence=0.87,
                    resolution_action="increase redis maxmemory",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=320_000,
                    evidence_keys=["search_logs", "get_golden_signals", "itsm_context"],
                    outcome="resolved",
                    tags=["redis", "cache-eviction", "sale-event"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2008",
                    service="cart-service",
                    incident_type="error_spike",
                    failure_signature="redis maxmemory eviction high traffic",
                    root_cause="Redis cache under memory pressure; eviction causing add-to-cart failures",
                    confidence=0.84,
                    resolution_action="increase redis maxmemory",
                    resolved_by="auto",
                    time_to_resolve_ms=280_000,
                    evidence_keys=["search_logs", "query_metrics"],
                    outcome="auto-remediated",
                    tags=["redis", "cache-eviction", "high-traffic"],
                    recorded_at=now_base,
                ),
                # 2x auth-service / timeout — JWT validation spike
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2009",
                    service="auth-service",
                    incident_type="timeout",
                    failure_signature="JWT validation spike CPU saturation replicas insufficient",
                    root_cause="JWT RS256 validation CPU-bound; single replica saturated at 10k req/s",
                    confidence=0.90,
                    resolution_action="scale auth-service to 4 replicas",
                    resolved_by="auto",
                    time_to_resolve_ms=180_000,
                    evidence_keys=["get_golden_signals", "query_metrics", "cmdb_blast_radius"],
                    outcome="auto-remediated",
                    tags=["jwt", "cpu-saturation", "scaling", "kubernetes"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2010",
                    service="auth-service",
                    incident_type="timeout",
                    failure_signature="JWT validation timeout high concurrency",
                    root_cause="Auth service overloaded; JWT validation latency >500ms causing cascade",
                    confidence=0.86,
                    resolution_action="scale auth-service to 4 replicas",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=200_000,
                    evidence_keys=["search_logs", "get_golden_signals", "itsm_context"],
                    outcome="resolved",
                    tags=["jwt", "scaling", "cascade"],
                    recorded_at=now_base,
                ),
                # 2x api-gateway / latency — upstream timeout cascade
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2011",
                    service="api-gateway",
                    incident_type="latency",
                    failure_signature="upstream timeout cascade circuit breaker threshold",
                    root_cause="Circuit breaker threshold too low (5%); legitimate retries tripping breaker",
                    confidence=0.88,
                    resolution_action="increase circuit breaker threshold",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=720_000,
                    evidence_keys=["search_logs", "get_golden_signals", "cmdb_blast_radius"],
                    outcome="resolved",
                    tags=["circuit-breaker", "upstream-timeout", "api-gateway"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2012",
                    service="api-gateway",
                    incident_type="latency",
                    failure_signature="upstream payment timeout cascade api latency",
                    root_cause="Payment service degradation causing upstream timeout cascade at api-gateway",
                    confidence=0.82,
                    resolution_action="increase circuit breaker threshold",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=750_000,
                    evidence_keys=["search_logs", "get_golden_signals", "itsm_context"],
                    outcome="resolved",
                    tags=["circuit-breaker", "cascade", "payment-dependency"],
                    recorded_at=now_base,
                ),
                # 2x order-service / cascading
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2013",
                    service="order-service",
                    incident_type="cascading",
                    failure_signature="payment-service degraded causing order failures cascade",
                    root_cause="payment-service timeout propagated to order-service; all order placements failing",
                    confidence=0.95,
                    resolution_action="auto-remediated by harness",
                    resolved_by="auto",
                    time_to_resolve_ms=120_000,
                    evidence_keys=["search_logs", "cmdb_blast_radius", "cascade_tracker"],
                    outcome="auto-remediated",
                    tags=["cascade", "payment-dependency", "order-service"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2014",
                    service="order-service",
                    incident_type="cascading",
                    failure_signature="payment-service degraded order placement failures",
                    root_cause="Downstream payment-service SLO breach cascading to order placement 5xx",
                    confidence=0.91,
                    resolution_action="auto-remediated by harness",
                    resolved_by="auto",
                    time_to_resolve_ms=110_000,
                    evidence_keys=["search_logs", "get_golden_signals", "cmdb_blast_radius"],
                    outcome="auto-remediated",
                    tags=["cascade", "payment-dependency"],
                    recorded_at=now_base,
                ),
                # Additional varied episodes
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2015",
                    service="inventory-service",
                    incident_type="error_spike",
                    failure_signature="mysql deadlock high write concurrency",
                    root_cause="MySQL deadlock on inventory_items table under concurrent write load",
                    confidence=0.79,
                    resolution_action="add row-level locking hint to batch update query",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=900_000,
                    evidence_keys=["search_logs", "query_metrics", "diff_analysis"],
                    outcome="resolved",
                    tags=["mysql", "deadlock", "concurrency"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2016",
                    service="notification-service",
                    incident_type="latency",
                    failure_signature="kafka consumer lag growing notification delay",
                    root_cause="Kafka consumer group lag at 500k messages; single partition bottleneck",
                    confidence=0.83,
                    resolution_action="increase kafka partition count",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=1_800_000,
                    evidence_keys=["search_logs", "get_golden_signals", "query_metrics"],
                    outcome="escalated",
                    tags=["kafka", "consumer-lag", "throughput"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2017",
                    service="search-service",
                    incident_type="error_spike",
                    failure_signature="elasticsearch heap pressure OOM errors",
                    root_cause="Elasticsearch JVM heap at 95%; heavy aggregation queries causing GC pauses",
                    confidence=0.86,
                    resolution_action="increase elasticsearch heap size",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=600_000,
                    evidence_keys=["search_logs", "get_golden_signals", "query_metrics"],
                    outcome="resolved",
                    tags=["elasticsearch", "heap", "oom"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2018",
                    service="payment-service",
                    incident_type="timeout",
                    failure_signature="postgres connection pool exhausted replica lag",
                    root_cause="Replica lag of 30s causing reads to overflow to primary, exhausting pool",
                    confidence=0.80,
                    resolution_action="increase pool size",
                    resolved_by="SRE-on-call",
                    time_to_resolve_ms=540_000,
                    evidence_keys=["search_logs", "query_metrics", "get_golden_signals"],
                    outcome="resolved",
                    tags=["database", "connection-pool", "replica-lag"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2019",
                    service="cart-service",
                    incident_type="error_spike",
                    failure_signature="redis connection refused eviction policy",
                    root_cause="Redis noeviction policy blocking writes when maxmemory exceeded",
                    confidence=0.77,
                    resolution_action="increase redis maxmemory",
                    resolved_by="unknown",
                    time_to_resolve_ms=350_000,
                    evidence_keys=["search_logs", "get_golden_signals"],
                    outcome="unknown",
                    tags=["redis", "noeviction", "memory"],
                    recorded_at=now_base,
                ),
                Episode(
                    episode_id=str(uuid.uuid4()),
                    incident_id="INC-2020",
                    service="api-gateway",
                    incident_type="error_spike",
                    failure_signature="rate limiter misconfiguration blocking legitimate traffic",
                    root_cause="Rate limiter config deployed with 100 req/min instead of 100 req/s; blocking users",
                    confidence=0.94,
                    resolution_action="rollback rate limiter config",
                    resolved_by="auto",
                    time_to_resolve_ms=90_000,
                    evidence_keys=["search_logs", "devops_context", "diff_analysis"],
                    outcome="auto-remediated",
                    tags=["rate-limiter", "misconfiguration", "deployment"],
                    recorded_at=now_base,
                ),
            ]

            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            for ep in demos:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ep.to_dict()) + "\n")
            self._episodes = demos
            self._index = None
            logger.info("EpisodicMemory: seeded %d demo episodes", len(demos))
        except Exception as exc:
            logger.warning("EpisodicMemory.seed_demo_episodes failed: %s", exc)
