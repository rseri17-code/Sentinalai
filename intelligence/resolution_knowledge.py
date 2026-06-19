"""Resolution Knowledge Base — tracks which remediation actions work for which failure modes.

Builds a resolution recommendation engine from historical records.

Storage: JSONL (append-only) at eval/resolution_knowledge.jsonl
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("sentinalai.intelligence.resolution_knowledge")

_DEFAULT_STORAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "eval", "resolution_knowledge.jsonl"
)


@dataclass
class ResolutionRecord:
    record_id: str
    failure_mode: str          # "connection_pool_exhausted" | "redis_eviction" | etc.
    incident_type: str
    service_tier: int          # 1-3
    action_taken: str          # "increase_pool_size" | "scale_deployment" | etc.
    action_description: str    # human-readable
    success: bool
    time_to_resolve_ms: int
    confidence_before: float
    confidence_after: float
    recorded_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ResolutionRecord":
        return cls(
            record_id=d.get("record_id", str(uuid.uuid4())),
            failure_mode=d.get("failure_mode", ""),
            incident_type=d.get("incident_type", ""),
            service_tier=int(d.get("service_tier", 2)),
            action_taken=d.get("action_taken", ""),
            action_description=d.get("action_description", ""),
            success=bool(d.get("success", False)),
            time_to_resolve_ms=int(d.get("time_to_resolve_ms", 0)),
            confidence_before=float(d.get("confidence_before", 0.0)),
            confidence_after=float(d.get("confidence_after", 0.0)),
            recorded_at=d.get("recorded_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class ResolutionRecommendation:
    action: str
    description: str
    success_rate: float        # historical success rate
    avg_ttresolve_ms: int
    times_tried: int
    confidence: float          # recommendation confidence

    def to_dict(self) -> dict:
        return asdict(self)


def _recency_weight(recorded_at: str, now_iso: str = "") -> float:
    """Simple recency weight: newer records score closer to 1.0."""
    try:
        from datetime import datetime, timezone
        rec_dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        age_days = (now_dt - rec_dt).total_seconds() / 86400
        # Decay: half-life ~90 days
        return max(0.1, 1.0 - (age_days / 180.0))
    except Exception:
        return 0.5


class ResolutionKnowledge:
    """JSONL-backed resolution recommendation engine."""

    def __init__(self, storage_path: str = _DEFAULT_STORAGE_PATH) -> None:
        self._path = storage_path
        self._records: list = []
        self._load()
        if not self._records:
            self.seed_demo_records()

    def _load(self) -> None:
        try:
            if not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._records.append(ResolutionRecord.from_dict(json.loads(line)))
                        except Exception as exc:
                            logger.debug("Skipping malformed record line: %s", exc)
        except Exception as exc:
            logger.warning("ResolutionKnowledge load failed: %s", exc)

    def record(self, rec: ResolutionRecord) -> None:
        """Append a resolution record to the JSONL store."""
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict()) + "\n")
            self._records.append(rec)
        except Exception as exc:
            logger.warning("ResolutionKnowledge.record failed: %s", exc)

    def recommend(
        self,
        failure_mode: str,
        incident_type: str,
        service_tier: int = 2,
    ) -> list:
        """Return top 3 recommendations ranked by success_rate * recency_weight."""
        try:
            # Filter relevant records
            candidates = [
                r for r in self._records
                if r.failure_mode == failure_mode or r.incident_type == incident_type
            ]

            if not candidates:
                return []

            # Aggregate by action_taken
            action_records: dict = {}
            for r in candidates:
                key = r.action_taken
                if key not in action_records:
                    action_records[key] = {
                        "records": [],
                        "description": r.action_description,
                    }
                action_records[key]["records"].append(r)

            recommendations = []
            for action, data in action_records.items():
                recs = data["records"]
                success_count = sum(1 for r in recs if r.success)
                success_rate = success_count / len(recs) if recs else 0.0
                avg_ttresolve = int(
                    sum(r.time_to_resolve_ms for r in recs) / len(recs)
                ) if recs else 0
                # Recency-weighted score
                weighted_score = success_rate * (
                    sum(_recency_weight(r.recorded_at) for r in recs) / len(recs)
                )
                # Recommendation confidence: dampen if fewer than 3 attempts
                confidence = min(1.0, success_rate * (1 - 1.0 / (len(recs) + 1)))
                recommendations.append((
                    weighted_score,
                    ResolutionRecommendation(
                        action=action,
                        description=data["description"],
                        success_rate=round(success_rate, 3),
                        avg_ttresolve_ms=avg_ttresolve,
                        times_tried=len(recs),
                        confidence=round(confidence, 3),
                    )
                ))

            recommendations.sort(key=lambda x: -x[0])
            return [r for _, r in recommendations[:3]]
        except Exception as exc:
            logger.warning("ResolutionKnowledge.recommend failed: %s", exc)
            return []

    def get_leaderboard(self, incident_type: str) -> list:
        """Return top actions for this incident type sorted by success rate."""
        try:
            candidates = [r for r in self._records if r.incident_type == incident_type]
            if not candidates:
                return []

            action_stats: dict = {}
            for r in candidates:
                key = r.action_taken
                if key not in action_stats:
                    action_stats[key] = {"success": 0, "total": 0, "description": r.action_description}
                action_stats[key]["total"] += 1
                if r.success:
                    action_stats[key]["success"] += 1

            leaderboard = []
            for action, stats in action_stats.items():
                leaderboard.append({
                    "action": action,
                    "description": stats["description"],
                    "success_rate": round(stats["success"] / stats["total"], 3),
                    "times_tried": stats["total"],
                })
            leaderboard.sort(key=lambda x: (-x["success_rate"], -x["times_tried"]))
            return leaderboard
        except Exception as exc:
            logger.warning("ResolutionKnowledge.get_leaderboard failed: %s", exc)
            return []

    def seed_demo_records(self) -> None:
        """Seed 30 realistic resolution records matching the episodic memory."""
        try:
            if os.path.exists(self._path):
                return

            now_base = "2026-06-01T00:00:00+00:00"

            demos = [
                # connection_pool_exhausted / timeout — 8 records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="connection_pool_exhausted",
                    incident_type="timeout",
                    service_tier=1,
                    action_taken="increase_pool_size",
                    action_description="Increase Postgres connection pool size from 20 to 50",
                    success=True,
                    time_to_resolve_ms=480_000,
                    confidence_before=0.75,
                    confidence_after=0.91,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="connection_pool_exhausted",
                    incident_type="timeout",
                    service_tier=1,
                    action_taken="increase_pool_size",
                    action_description="Increase Postgres connection pool size from 20 to 50",
                    success=True,
                    time_to_resolve_ms=510_000,
                    confidence_before=0.80,
                    confidence_after=0.88,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="connection_pool_exhausted",
                    incident_type="timeout",
                    service_tier=1,
                    action_taken="increase_pool_size",
                    action_description="Increase Postgres connection pool size from 20 to 50",
                    success=True,
                    time_to_resolve_ms=460_000,
                    confidence_before=0.78,
                    confidence_after=0.85,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="connection_pool_exhausted",
                    incident_type="timeout",
                    service_tier=1,
                    action_taken="increase_pool_size",
                    action_description="Increase Postgres connection pool size from 20 to 50",
                    success=True,
                    time_to_resolve_ms=540_000,
                    confidence_before=0.72,
                    confidence_after=0.80,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="connection_pool_exhausted",
                    incident_type="timeout",
                    service_tier=2,
                    action_taken="scale_deployment",
                    action_description="Scale service replicas to reduce per-replica connection demand",
                    success=False,
                    time_to_resolve_ms=900_000,
                    confidence_before=0.60,
                    confidence_after=0.55,
                    recorded_at=now_base,
                ),
                # slow_postgres_query / latency — 5 records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="slow_postgres_query",
                    incident_type="latency",
                    service_tier=1,
                    action_taken="add_database_index",
                    action_description="Add index on orders.created_at to eliminate sequential scan",
                    success=True,
                    time_to_resolve_ms=1_500_000,
                    confidence_before=0.85,
                    confidence_after=0.93,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="slow_postgres_query",
                    incident_type="latency",
                    service_tier=1,
                    action_taken="add_database_index",
                    action_description="Add index on orders.created_at to eliminate sequential scan",
                    success=True,
                    time_to_resolve_ms=1_620_000,
                    confidence_before=0.81,
                    confidence_after=0.89,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="slow_postgres_query",
                    incident_type="latency",
                    service_tier=2,
                    action_taken="optimize_query",
                    action_description="Rewrite query to use indexed columns and add LIMIT clause",
                    success=True,
                    time_to_resolve_ms=1_200_000,
                    confidence_before=0.70,
                    confidence_after=0.82,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="slow_postgres_query",
                    incident_type="latency",
                    service_tier=2,
                    action_taken="increase_pool_size",
                    action_description="Increase connection pool size as a temporary workaround",
                    success=False,
                    time_to_resolve_ms=2_000_000,
                    confidence_before=0.50,
                    confidence_after=0.45,
                    recorded_at=now_base,
                ),
                # redis_eviction / error_spike — 7 records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="redis_eviction",
                    incident_type="error_spike",
                    service_tier=2,
                    action_taken="increase_redis_maxmemory",
                    action_description="Increase Redis maxmemory from 512MB to 2GB",
                    success=True,
                    time_to_resolve_ms=300_000,
                    confidence_before=0.82,
                    confidence_after=0.92,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="redis_eviction",
                    incident_type="error_spike",
                    service_tier=2,
                    action_taken="increase_redis_maxmemory",
                    action_description="Increase Redis maxmemory from 512MB to 2GB",
                    success=True,
                    time_to_resolve_ms=320_000,
                    confidence_before=0.78,
                    confidence_after=0.87,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="redis_eviction",
                    incident_type="error_spike",
                    service_tier=2,
                    action_taken="increase_redis_maxmemory",
                    action_description="Increase Redis maxmemory from 512MB to 2GB",
                    success=True,
                    time_to_resolve_ms=280_000,
                    confidence_before=0.75,
                    confidence_after=0.84,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="redis_eviction",
                    incident_type="error_spike",
                    service_tier=2,
                    action_taken="increase_redis_maxmemory",
                    action_description="Increase Redis maxmemory from 512MB to 2GB",
                    success=True,
                    time_to_resolve_ms=350_000,
                    confidence_before=0.68,
                    confidence_after=0.77,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="redis_eviction",
                    incident_type="error_spike",
                    service_tier=3,
                    action_taken="restart_redis",
                    action_description="Restart Redis to clear eviction backlog",
                    success=False,
                    time_to_resolve_ms=600_000,
                    confidence_before=0.55,
                    confidence_after=0.40,
                    recorded_at=now_base,
                ),
                # jwt_validation_spike / timeout — 4 records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="jwt_validation_spike",
                    incident_type="timeout",
                    service_tier=1,
                    action_taken="scale_deployment",
                    action_description="Scale auth-service from 1 to 4 replicas to distribute JWT validation load",
                    success=True,
                    time_to_resolve_ms=180_000,
                    confidence_before=0.82,
                    confidence_after=0.90,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="jwt_validation_spike",
                    incident_type="timeout",
                    service_tier=1,
                    action_taken="scale_deployment",
                    action_description="Scale auth-service from 1 to 4 replicas to distribute JWT validation load",
                    success=True,
                    time_to_resolve_ms=200_000,
                    confidence_before=0.79,
                    confidence_after=0.86,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="jwt_validation_spike",
                    incident_type="timeout",
                    service_tier=2,
                    action_taken="enable_jwt_cache",
                    action_description="Enable JWT validation result caching to reduce CPU load",
                    success=True,
                    time_to_resolve_ms=150_000,
                    confidence_before=0.70,
                    confidence_after=0.85,
                    recorded_at=now_base,
                ),
                # upstream_timeout_cascade / latency — 4 records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="upstream_timeout_cascade",
                    incident_type="latency",
                    service_tier=1,
                    action_taken="increase_circuit_breaker_threshold",
                    action_description="Increase circuit breaker error threshold from 5% to 25%",
                    success=True,
                    time_to_resolve_ms=720_000,
                    confidence_before=0.80,
                    confidence_after=0.88,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="upstream_timeout_cascade",
                    incident_type="latency",
                    service_tier=1,
                    action_taken="increase_circuit_breaker_threshold",
                    action_description="Increase circuit breaker error threshold from 5% to 25%",
                    success=True,
                    time_to_resolve_ms=750_000,
                    confidence_before=0.74,
                    confidence_after=0.82,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="upstream_timeout_cascade",
                    incident_type="latency",
                    service_tier=2,
                    action_taken="increase_upstream_timeout",
                    action_description="Increase HTTP client timeout from 1s to 5s",
                    success=False,
                    time_to_resolve_ms=1_200_000,
                    confidence_before=0.55,
                    confidence_after=0.45,
                    recorded_at=now_base,
                ),
                # payment_cascade / cascading — 4 records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="payment_cascade",
                    incident_type="cascading",
                    service_tier=1,
                    action_taken="auto_remediation_harness",
                    action_description="Automated harness detected cascade and triggered circuit isolation",
                    success=True,
                    time_to_resolve_ms=120_000,
                    confidence_before=0.90,
                    confidence_after=0.95,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="payment_cascade",
                    incident_type="cascading",
                    service_tier=1,
                    action_taken="auto_remediation_harness",
                    action_description="Automated harness detected cascade and triggered circuit isolation",
                    success=True,
                    time_to_resolve_ms=110_000,
                    confidence_before=0.88,
                    confidence_after=0.91,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="payment_cascade",
                    incident_type="cascading",
                    service_tier=2,
                    action_taken="manual_circuit_break",
                    action_description="Manually disable payment-service dependency in order-service config",
                    success=True,
                    time_to_resolve_ms=300_000,
                    confidence_before=0.75,
                    confidence_after=0.88,
                    recorded_at=now_base,
                ),
                # Additional varied records
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="mysql_deadlock",
                    incident_type="error_spike",
                    service_tier=2,
                    action_taken="add_row_level_locking",
                    action_description="Add FOR UPDATE SKIP LOCKED to batch query to avoid deadlocks",
                    success=True,
                    time_to_resolve_ms=900_000,
                    confidence_before=0.72,
                    confidence_after=0.79,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="kafka_consumer_lag",
                    incident_type="latency",
                    service_tier=2,
                    action_taken="increase_kafka_partitions",
                    action_description="Increase Kafka partition count from 1 to 6 for parallelism",
                    success=False,
                    time_to_resolve_ms=1_800_000,
                    confidence_before=0.65,
                    confidence_after=0.83,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="elasticsearch_heap_pressure",
                    incident_type="error_spike",
                    service_tier=2,
                    action_taken="increase_jvm_heap",
                    action_description="Increase Elasticsearch JVM heap from 2GB to 8GB",
                    success=True,
                    time_to_resolve_ms=600_000,
                    confidence_before=0.79,
                    confidence_after=0.86,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="rate_limiter_misconfiguration",
                    incident_type="error_spike",
                    service_tier=1,
                    action_taken="rollback_config",
                    action_description="Rollback rate limiter config to last known good state",
                    success=True,
                    time_to_resolve_ms=90_000,
                    confidence_before=0.88,
                    confidence_after=0.94,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="connection_pool_exhausted",
                    incident_type="timeout",
                    service_tier=2,
                    action_taken="increase_pool_size",
                    action_description="Increase Postgres connection pool size from 10 to 30",
                    success=True,
                    time_to_resolve_ms=490_000,
                    confidence_before=0.76,
                    confidence_after=0.84,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="redis_eviction",
                    incident_type="error_spike",
                    service_tier=1,
                    action_taken="increase_redis_maxmemory",
                    action_description="Increase Redis maxmemory from 1GB to 4GB",
                    success=True,
                    time_to_resolve_ms=310_000,
                    confidence_before=0.80,
                    confidence_after=0.90,
                    recorded_at=now_base,
                ),
                ResolutionRecord(
                    record_id=str(uuid.uuid4()),
                    failure_mode="slow_postgres_query",
                    incident_type="latency",
                    service_tier=1,
                    action_taken="add_database_index",
                    action_description="Add composite index on (user_id, created_at) to accelerate lookups",
                    success=True,
                    time_to_resolve_ms=1_400_000,
                    confidence_before=0.83,
                    confidence_after=0.91,
                    recorded_at=now_base,
                ),
            ]

            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            for rec in demos:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec.to_dict()) + "\n")
            self._records = demos
            logger.info("ResolutionKnowledge: seeded %d demo records", len(demos))
        except Exception as exc:
            logger.warning("ResolutionKnowledge.seed_demo_records failed: %s", exc)
