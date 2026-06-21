"""Causal Intelligence Graph — temporal, weighted service topology.

Stores a service dependency graph with learned failure-correlation weights.
Persisted as JSONL at eval/causal_graph.jsonl.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

logger = logging.getLogger("sentinalai.causal_graph")

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "eval", "causal_graph.jsonl"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ServiceNode:
    service_id: str
    display_name: str
    team: str
    tier: int                   # 1=critical, 2=important, 3=standard
    health: float               # 0.0-1.0
    alert_count: int
    last_incident_ts: str
    technologies: list


@dataclass
class CausalEdge:
    source: str
    target: str
    edge_type: str              # "calls" | "depends_on" | "publishes_to" | "reads_from"
    call_volume: float          # 0.0-1.0 normalized
    failure_correlation: float  # P(target fails | source fails)
    avg_propagation_ms: int
    observed_count: int
    last_updated: str


@dataclass
class BlastRadiusResult:
    origin_service: str
    affected: list              # [{"service_id", "probability", "propagation_ms", "path"}]
    total_affected: int
    severity: str               # "critical" | "high" | "medium" | "low"


# EMA smoothing factor for correlation updates
_EMA_ALPHA = 0.3


class CausalGraph:
    """In-memory + JSONL-backed causal service graph."""

    def __init__(self, storage_path: str = _DEFAULT_PATH) -> None:
        self._path = storage_path
        self._nodes: dict[str, ServiceNode] = {}
        self._edges: dict[tuple, CausalEdge] = {}
        self._load()
        if not self._nodes:
            self.seed_demo_topology()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_topology(self) -> dict:
        """Return full graph as {"nodes": [...], "edges": [...]}."""
        try:
            return {
                "nodes": [asdict(n) for n in self._nodes.values()],
                "edges": [asdict(e) for e in self._edges.values()],
            }
        except Exception as exc:
            logger.warning("get_topology failed: %s", exc)
            return {"nodes": [], "edges": []}

    def get_blast_radius(self, service_id: str) -> BlastRadiusResult:
        """BFS from service_id, weighted by failure_correlation."""
        try:
            if service_id not in self._nodes:
                return BlastRadiusResult(
                    origin_service=service_id,
                    affected=[],
                    total_affected=0,
                    severity="low",
                )

            # Build adjacency list: source -> [edges]
            adj: dict[str, list] = {}
            for edge in self._edges.values():
                adj.setdefault(edge.source, []).append(edge)

            # BFS with accumulated probability
            visited: dict[str, dict] = {}
            queue: deque = deque()
            queue.append((service_id, 1.0, 0, [service_id]))

            while queue:
                current, prob, prop_ms, path = queue.popleft()
                for edge in adj.get(current, []):
                    target = edge.target
                    if target == service_id:
                        continue
                    new_prob = prob * edge.failure_correlation
                    new_ms = prop_ms + edge.avg_propagation_ms
                    new_path = path + [target]
                    if target not in visited or visited[target]["probability"] < new_prob:
                        visited[target] = {
                            "service_id": target,
                            "probability": round(new_prob, 4),
                            "propagation_ms": new_ms,
                            "path": new_path,
                        }
                        if new_prob > 0.05:
                            queue.append((target, new_prob, new_ms, new_path))

            affected = sorted(
                visited.values(), key=lambda x: x["probability"], reverse=True
            )

            # Determine severity
            origin_node = self._nodes[service_id]
            max_prob = affected[0]["probability"] if affected else 0.0
            if origin_node.tier == 1 and len(affected) >= 3:
                severity = "critical"
            elif max_prob >= 0.7 or len(affected) >= 5:
                severity = "high"
            elif max_prob >= 0.4 or len(affected) >= 2:
                severity = "medium"
            else:
                severity = "low"

            return BlastRadiusResult(
                origin_service=service_id,
                affected=affected,
                total_affected=len(affected),
                severity=severity,
            )
        except Exception as exc:
            logger.warning("get_blast_radius failed: %s", exc)
            return BlastRadiusResult(
                origin_service=service_id,
                affected=[],
                total_affected=0,
                severity="low",
            )

    def record_co_failure(
        self, source: str, target: str, propagation_ms: int
    ) -> None:
        """Update edge weights using exponential moving average."""
        try:
            key = (source, target)
            if key not in self._edges:
                self._edges[key] = CausalEdge(
                    source=source,
                    target=target,
                    edge_type="calls",
                    call_volume=0.5,
                    failure_correlation=0.1,
                    avg_propagation_ms=propagation_ms,
                    observed_count=1,
                    last_updated=_now_iso(),
                )
            else:
                edge = self._edges[key]
                edge.failure_correlation = round(
                    _EMA_ALPHA * 1.0 + (1 - _EMA_ALPHA) * edge.failure_correlation, 4
                )
                edge.avg_propagation_ms = int(
                    _EMA_ALPHA * propagation_ms
                    + (1 - _EMA_ALPHA) * edge.avg_propagation_ms
                )
                edge.observed_count += 1
                edge.last_updated = _now_iso()
            self._save()
        except Exception as exc:
            logger.warning("record_co_failure failed (non-critical): %s", exc)

    def update_service_health(
        self, service_id: str, health: float, alert_count: int
    ) -> None:
        """Update health metrics for a service."""
        try:
            if service_id in self._nodes:
                node = self._nodes[service_id]
                node.health = max(0.0, min(1.0, health))
                node.alert_count = alert_count
                node.last_incident_ts = _now_iso()
            else:
                self._nodes[service_id] = ServiceNode(
                    service_id=service_id,
                    display_name=service_id.replace("-", " ").title(),
                    team="unknown-team",
                    tier=3,
                    health=max(0.0, min(1.0, health)),
                    alert_count=alert_count,
                    last_incident_ts=_now_iso(),
                    technologies=[],
                )
            self._save()
        except Exception as exc:
            logger.warning("update_service_health failed (non-critical): %s", exc)

    def seed_demo_topology(self) -> None:
        """Seed 16 realistic demo services and 15 edges if graph is empty."""
        try:
            ts = _now_iso()

            nodes = [
                ServiceNode("api-gateway", "API Gateway", "platform-team", 1, 0.95, 0, ts, ["nginx", "lua"]),
                ServiceNode("auth-service", "Auth Service", "platform-team", 1, 0.97, 0, ts, ["python", "redis", "jwt"]),
                ServiceNode("payment-service", "Payment Service", "payments-team", 1, 0.72, 2, ts, ["java", "postgres", "kafka"]),
                ServiceNode("order-service", "Order Service", "commerce-team", 2, 0.91, 0, ts, ["python", "postgres", "redis"]),
                ServiceNode("inventory-service", "Inventory Service", "commerce-team", 2, 0.95, 0, ts, ["go", "postgres"]),
                ServiceNode("notification-service", "Notification Service", "platform-team", 3, 0.98, 0, ts, ["python", "kafka", "sendgrid"]),
                ServiceNode("user-service", "User Service", "identity-team", 2, 0.96, 0, ts, ["python", "postgres"]),
                ServiceNode("cart-service", "Cart Service", "commerce-team", 2, 0.68, 1, ts, ["node", "redis"]),
                ServiceNode("search-service", "Search Service", "search-team", 2, 0.94, 0, ts, ["python", "elasticsearch"]),
                ServiceNode("recommendation-engine", "Recommendation Engine", "ml-team", 3, 0.99, 0, ts, ["python", "redis", "tensorflow"]),
                ServiceNode("postgres-primary", "Postgres Primary", "data-team", 1, 0.88, 1, ts, ["postgres"]),
                ServiceNode("redis-cluster", "Redis Cluster", "data-team", 1, 0.91, 0, ts, ["redis"]),
                ServiceNode("kafka-broker", "Kafka Broker", "data-team", 1, 0.96, 0, ts, ["kafka", "zookeeper"]),
                ServiceNode("cdn-edge", "CDN Edge", "platform-team", 2, 0.99, 0, ts, ["cloudfront", "nginx"]),
                ServiceNode("mobile-bff", "Mobile BFF", "mobile-team", 2, 0.93, 0, ts, ["node", "graphql"]),
                ServiceNode("elasticsearch-cluster", "Elasticsearch Cluster", "search-team", 2, 0.97, 0, ts, ["elasticsearch", "kibana"]),
            ]
            for node in nodes:
                self._nodes[node.service_id] = node

            edges_raw = [
                ("api-gateway", "auth-service", "calls", 1.0, 0.85, 45),
                ("api-gateway", "mobile-bff", "calls", 0.6, 0.70, 30),
                ("mobile-bff", "payment-service", "calls", 0.4, 0.80, 120),
                ("payment-service", "postgres-primary", "depends_on", 0.9, 0.95, 15),
                ("payment-service", "order-service", "calls", 0.7, 0.75, 80),
                ("order-service", "inventory-service", "calls", 0.8, 0.65, 60),
                ("order-service", "notification-service", "calls", 0.5, 0.40, 200),
                ("order-service", "kafka-broker", "publishes_to", 0.7, 0.55, 25),
                ("cart-service", "redis-cluster", "depends_on", 0.95, 0.90, 10),
                ("cart-service", "inventory-service", "calls", 0.6, 0.60, 55),
                ("search-service", "elasticsearch-cluster", "depends_on", 1.0, 0.88, 20),
                ("recommendation-engine", "redis-cluster", "reads_from", 0.8, 0.70, 12),
                ("user-service", "postgres-primary", "depends_on", 0.85, 0.92, 15),
                ("auth-service", "redis-cluster", "reads_from", 0.9, 0.80, 8),
                ("cdn-edge", "api-gateway", "calls", 1.0, 0.60, 20),
            ]
            for src, tgt, etype, vol, corr, ms in edges_raw:
                key = (src, tgt)
                self._edges[key] = CausalEdge(
                    source=src,
                    target=tgt,
                    edge_type=etype,
                    call_volume=vol,
                    failure_correlation=corr,
                    avg_propagation_ms=ms,
                    observed_count=0,
                    last_updated=ts,
                )
            self._save()
            logger.info(
                "Seeded demo topology: %d nodes, %d edges",
                len(self._nodes), len(self._edges),
            )
        except Exception as exc:
            logger.warning("seed_demo_topology failed (non-critical): %s", exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load graph from JSONL file."""
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    rtype = record.get("_type")
                    data = record.get("data", {})
                    if rtype == "node":
                        n = ServiceNode(**data)
                        self._nodes[n.service_id] = n
                    elif rtype == "edge":
                        e = CausalEdge(**data)
                        self._edges[(e.source, e.target)] = e
        except Exception as exc:
            logger.warning("CausalGraph load failed (non-critical): %s", exc)

    def _save(self) -> None:
        """Persist graph to JSONL file (full rewrite)."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                for node in self._nodes.values():
                    f.write(json.dumps({"_type": "node", "data": asdict(node)}) + "\n")
                for edge in self._edges.values():
                    f.write(json.dumps({"_type": "edge", "data": asdict(edge)}) + "\n")
        except Exception as exc:
            logger.warning("CausalGraph save failed (non-critical): %s", exc)
