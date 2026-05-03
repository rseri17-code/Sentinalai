"""Telemetry Aggregator — continuous metric collection from all MCP sources.

Polls all connected MCP workers on a configurable schedule and normalises
their output into a uniform TelemetrySnapshot written to PostgreSQL.

Design decisions:
  - Polling first (works with existing MCP stubs, simpler to test)
  - Pluggable transport: swap poll() for a streaming handler per source later
  - PostgreSQL storage: reuses existing DB, 7-day rolling window by default
  - Per-service priority: critical services polled more frequently
  - Cold-start awareness: tracks observation count per service so downstream
    components know when baselines are reliable

Schema: telemetry_snapshots table (created by schema.sql)

Configuration:
  TELEMETRY_POLL_INTERVAL_SEC   default 60
  TELEMETRY_RETENTION_DAYS      default 7
  TELEMETRY_ENABLED             default true
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.telemetry_aggregator")

TELEMETRY_POLL_INTERVAL_SEC = int(os.environ.get("TELEMETRY_POLL_INTERVAL_SEC", "60"))
TELEMETRY_RETENTION_DAYS    = int(os.environ.get("TELEMETRY_RETENTION_DAYS", "7"))
TELEMETRY_ENABLED           = os.environ.get("TELEMETRY_ENABLED", "true").lower() in ("1", "true", "yes")
# Max wall-clock seconds to wait for any single service's MCP calls before skipping
COLLECT_TIMEOUT_SEC         = int(os.environ.get("TELEMETRY_COLLECT_TIMEOUT_SEC", "10"))

# Minimum observations before pattern detector trusts this service's baseline
MIN_OBSERVATIONS_FOR_BASELINE = 24   # 24 × 60s = 24 minutes minimum; 1440 = 24h recommended


@dataclass
class TelemetrySnapshot:
    """Normalised point-in-time metric snapshot for one service."""
    service: str
    source: str                      # sysdig | dynatrace | signalfx | splunk
    collected_at: str                # ISO8601 UTC
    collected_at_epoch: float        # Unix timestamp for time-series queries

    # Golden signals
    error_rate: float = 0.0          # fraction 0.0–1.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    request_rate_rps: float = 0.0
    saturation_pct: float = 0.0      # CPU / memory / connection pool — highest of available

    # Resource metrics
    cpu_pct: float = 0.0
    memory_pct: float = 0.0
    disk_pct: float = 0.0
    connection_pool_pct: float = 0.0

    # Log signals
    error_log_rate: float = 0.0      # error log lines per second
    new_error_signatures: int = 0    # distinct new error patterns vs. prior window

    # Deployment context
    recent_deploy: bool = False      # deploy in last 30 minutes?
    deploy_age_minutes: float = -1   # -1 = no recent deploy

    # Meta
    raw: dict = field(default_factory=dict, repr=False)  # original MCP response

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


class TelemetryAggregator:
    """Polls MCP workers and persists TelemetrySnapshots to PostgreSQL."""

    def __init__(self) -> None:
        self._workers: dict[str, Any] = {}   # lazily initialised
        self._observation_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect_all(self, services: list[str] | None = None) -> list[TelemetrySnapshot]:
        """Collect one snapshot per service from all available MCP workers.

        Args:
            services: explicit list to collect; None = auto-discover from KG

        Returns list of collected snapshots (persisted to DB as a side effect).
        """
        if not TELEMETRY_ENABLED:
            return []

        if services is None:
            services = self._discover_services()

        snapshots: list[TelemetrySnapshot] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._collect_service, svc): svc for svc in services}
            for future in concurrent.futures.as_completed(futures, timeout=COLLECT_TIMEOUT_SEC * len(services)):
                svc = futures[future]
                try:
                    snap = future.result(timeout=COLLECT_TIMEOUT_SEC)
                    if snap:
                        snapshots.append(snap)
                        self._persist(snap)
                        self._observation_counts[svc] = (
                            self._observation_counts.get(svc, 0) + 1
                        )
                except concurrent.futures.TimeoutError:
                    logger.warning("Telemetry collection timed out for service: %s", svc)
                except Exception as exc:
                    logger.warning("Telemetry collection failed for %s: %s", svc, exc)

        logger.debug("Telemetry collected: %d services", len(snapshots))
        return snapshots

    def get_recent(
        self,
        service: str,
        minutes: int = 60,
        metric: str = "error_rate",
    ) -> list[tuple[float, float]]:
        """Return (epoch, value) pairs for a metric over the last N minutes.

        Used by pattern_detector for trend and correlation analysis.
        Returns empty list if DB unavailable or insufficient data.
        """
        from database.persistence import get_engine
        engine = get_engine()
        if engine is None:
            return []

        try:
            from sqlalchemy import text
            since = time.time() - minutes * 60
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT collected_at_epoch, CAST(metrics->>:metric AS FLOAT)
                    FROM telemetry_snapshots
                    WHERE service = :service
                      AND collected_at_epoch >= :since
                      AND metrics->>:metric IS NOT NULL
                    ORDER BY collected_at_epoch ASC
                """), {"service": service, "metric": metric, "since": since})
                return [(float(r[0]), float(r[1])) for r in rows.fetchall() if r[1] is not None]
        except Exception as exc:
            logger.debug("get_recent failed for %s/%s: %s", service, metric, exc)
            return []

    def get_observation_count(self, service: str) -> int:
        """Return how many snapshots have been collected for this service.

        Values < MIN_OBSERVATIONS_FOR_BASELINE mean the baseline is not yet reliable.
        """
        cached = self._observation_counts.get(service)
        if cached is not None:
            return cached
        # Query DB for count
        from database.persistence import get_engine
        engine = get_engine()
        if engine is None:
            return 0
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                result = conn.execute(text(
                    "SELECT COUNT(*) FROM telemetry_snapshots WHERE service = :s"
                ), {"s": service})
                row = result.fetchone()
                count = int(row[0]) if row else 0
                self._observation_counts[service] = count
                return count
        except Exception:
            return 0

    def is_baseline_ready(self, service: str) -> bool:
        return self.get_observation_count(service) >= MIN_OBSERVATIONS_FOR_BASELINE

    def get_monitored_services(self) -> list[str]:
        """Public accessor for service discovery (used by background runner)."""
        return self._discover_services()

    def prune_old_snapshots(self) -> int:
        """Delete snapshots older than TELEMETRY_RETENTION_DAYS. Returns rows deleted."""
        from database.persistence import get_engine
        engine = get_engine()
        if engine is None:
            return 0
        try:
            from sqlalchemy import text
            cutoff = time.time() - TELEMETRY_RETENTION_DAYS * 86400
            with engine.connect() as conn:
                result = conn.execute(text(
                    "DELETE FROM telemetry_snapshots WHERE collected_at_epoch < :cutoff"
                ), {"cutoff": cutoff})
                conn.commit()
                return result.rowcount
        except Exception as exc:
            logger.warning("Prune failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _collect_service(self, service: str) -> TelemetrySnapshot | None:
        now = datetime.now(timezone.utc)
        snap = TelemetrySnapshot(
            service=service,
            source="mcp",
            collected_at=now.isoformat(),
            collected_at_epoch=now.timestamp(),
        )

        self._apply_metrics(snap, service)
        self._apply_logs(snap, service)
        self._apply_deploy_context(snap, service)
        return snap

    def _apply_metrics(self, snap: TelemetrySnapshot, service: str) -> None:
        """Fetch golden signals from metrics MCP."""
        try:
            from workers.mcp_client import MCPClient
            result = MCPClient().call("sysdig.golden_signals", {
                "service": service, "window_minutes": 5,
            })
            m = result.get("metrics", result)
            snap.error_rate       = float(m.get("error_rate", m.get("error_pct", 0)) or 0)
            snap.latency_p50_ms   = float(m.get("latency_p50", m.get("p50_ms", 0)) or 0)
            snap.latency_p95_ms   = float(m.get("latency_p95", m.get("p95_ms", 0)) or 0)
            snap.latency_p99_ms   = float(m.get("latency_p99", m.get("p99_ms", 0)) or 0)
            snap.request_rate_rps = float(m.get("request_rate", m.get("rps", 0)) or 0)
            snap.cpu_pct          = float(m.get("cpu_pct", m.get("cpu", 0)) or 0)
            snap.memory_pct       = float(m.get("memory_pct", m.get("memory", 0)) or 0)
            snap.connection_pool_pct = float(m.get("connection_pool_pct", 0) or 0)
            snap.saturation_pct   = max(snap.cpu_pct, snap.memory_pct, snap.connection_pool_pct)
            snap.raw["metrics"] = m
        except Exception as exc:
            logger.debug("Metrics collection failed for %s: %s", service, exc)

    def _apply_logs(self, snap: TelemetrySnapshot, service: str) -> None:
        """Fetch error log rate from log MCP."""
        try:
            from workers.mcp_client import MCPClient
            result = MCPClient().call("splunk.get_health_status", {
                "service": service, "window_minutes": 5,
            })
            snap.error_log_rate       = float(result.get("error_rate", 0) or 0)
            snap.new_error_signatures = int(result.get("new_patterns", 0) or 0)
            snap.raw["logs"] = result
        except Exception as exc:
            logger.debug("Log collection failed for %s: %s", service, exc)

    def _apply_deploy_context(self, snap: TelemetrySnapshot, service: str) -> None:
        """Check for recent deployments via GitHub MCP."""
        try:
            from workers.mcp_client import MCPClient
            result = MCPClient().call("github.get_recent_deployments", {
                "service": service, "limit": 1,
            })
            deploys = result.get("deployments", result.get("items", []))
            if deploys:
                latest = deploys[0]
                deploy_time = latest.get("created_at", latest.get("timestamp", ""))
                if deploy_time:
                    try:
                        ts = datetime.fromisoformat(deploy_time.replace("Z", "+00:00"))
                        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                        snap.deploy_age_minutes = round(age_min, 1)
                        snap.recent_deploy = age_min <= 30
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("Deploy context failed for %s: %s", service, exc)

    # ------------------------------------------------------------------
    # Service discovery
    # ------------------------------------------------------------------

    def _discover_services(self) -> list[str]:
        """Discover monitored services from the KG or fall back to env var."""
        services: list[str] = []
        try:
            from supervisor.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph.get_graph()
            for node in kg.get("nodes", []):
                if node.get("node_type") == "service":
                    label = node.get("label", "")
                    if label and label not in services:
                        services.append(label)
        except Exception:
            pass

        env_services = os.environ.get("MONITORED_SERVICES", "")
        if env_services:
            for s in env_services.split(","):
                s = s.strip()
                if s and s not in services:
                    services.append(s)

        if not services:
            logger.debug("No services discovered — using defaults")
            services = ["api-gateway", "payment-service", "auth-service"]

        return services

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, snap: TelemetrySnapshot) -> None:
        from database.persistence import get_engine
        engine = get_engine()
        if engine is None:
            return
        try:
            import json
            from sqlalchemy import text
            metrics = {k: v for k, v in snap.to_dict().items()
                       if k not in ("service", "source", "collected_at", "collected_at_epoch")}
            with engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO telemetry_snapshots
                        (service, source, collected_at, collected_at_epoch, metrics)
                    VALUES
                        (:service, :source, :collected_at, :epoch, :metrics::jsonb)
                """), {
                    "service": snap.service,
                    "source": snap.source,
                    "collected_at": snap.collected_at,
                    "epoch": snap.collected_at_epoch,
                    "metrics": json.dumps(metrics),
                })
                conn.commit()
        except Exception as exc:
            logger.debug("Persist snapshot failed for %s: %s", snap.service, exc)
