"""Staged Splunk retrieval planning for SentinalAI.

Implements a 3-stage progressive retrieval funnel that avoids broad brute-force
scans and targets the highest-signal indexes first.

Stage 1 — High-signal targeted (always run):
  Application logs, EKS/container logs, DB logs, auth/cert logs, middleware logs

Stage 2 — Dependency-specific (run when Stage 1 is inconclusive):
  Kafka, PingFederate, CyberArk, DNS, AppViewX, Oracle, Ansible, process activity

Stage 3 — Fallback broader correlation (run only if stages 1+2 weak):
  Broader error correlation, network events, infra automation, config drift

Budget-aware: stops early when decisive evidence is found (confidence >= threshold).
Each stage returns a list of query specs compatible with log_worker and metric_worker.

Design principles:
  - NEVER broad brute-force scans
  - Progressive funnel: narrow → dependency-specific → broad
  - Stop early when decisive evidence found
  - Degrade gracefully: return partial results if budget exhausted

Configuration:
  SPLUNK_STAGE1_THRESHOLD  — Confidence threshold to skip Stage 2 (default: 0.75)
  SPLUNK_STAGE2_THRESHOLD  — Confidence threshold to skip Stage 3 (default: 0.55)
  SPLUNK_MAX_QUERIES       — Max queries per plan (default: 15)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("sentinalai.splunk_retrieval_planner")

_STAGE1_THRESHOLD = float(os.environ.get("SPLUNK_STAGE1_THRESHOLD", "0.75"))
_STAGE2_THRESHOLD = float(os.environ.get("SPLUNK_STAGE2_THRESHOLD", "0.55"))
_MAX_QUERIES      = int(os.environ.get("SPLUNK_MAX_QUERIES", "15"))

# ---------------------------------------------------------------------------
# Source routing — configurable per deployment via env vars.
# Defaults preserve previous hardcoded behaviour.
# Override in production: SPLUNK_SOURCE_K8S=index=kubernetes sourcetype=kube:*
# ---------------------------------------------------------------------------
_SOURCE_MAP: dict[str, str] = {
    "eks_logs":      os.environ.get("SPLUNK_SOURCE_K8S",        "eks_logs"),
    "cert_logs":     os.environ.get("SPLUNK_SOURCE_CERT",       "cert_logs"),
    "infra_logs":    os.environ.get("SPLUNK_SOURCE_INFRA",      "infra_logs"),
    "auth_logs":     os.environ.get("SPLUNK_SOURCE_AUTH",       "auth_logs"),
    "cyberark_logs": os.environ.get("SPLUNK_SOURCE_CYBERARK",   "cyberark_logs"),
    "dns_logs":      os.environ.get("SPLUNK_SOURCE_DNS",        "dns_logs"),
    "db_logs":       os.environ.get("SPLUNK_SOURCE_DB",         "db_logs"),
    "kafka_logs":    os.environ.get("SPLUNK_SOURCE_KAFKA",      "kafka_logs"),
    "process_logs":  os.environ.get("SPLUNK_SOURCE_PROCESS",    "process_logs"),
}

def _src(key: str) -> str:
    """Resolve a logical source name to its environment-configured value."""
    return _SOURCE_MAP.get(key, key)


# ---------------------------------------------------------------------------
# Query spec
# ---------------------------------------------------------------------------

@dataclass
class SplunkQuery:
    """A single Splunk/log query specification."""
    stage: int
    worker: str
    action: str
    params: dict
    priority: int = 0        # lower = higher priority within stage
    signal_type: str = ""    # "logs", "metrics", "events", "changes"

    def to_gap_query(self) -> dict:
        """Convert to self_critique-compatible gap query dict."""
        return {
            "worker": self.worker,
            "action": self.action,
            "params": self.params,
        }


@dataclass
class RetrievalPlan:
    """Staged retrieval plan for a single investigation."""
    incident_type: str
    service: str
    stage1_queries: list[SplunkQuery] = field(default_factory=list)
    stage2_queries: list[SplunkQuery] = field(default_factory=list)
    stage3_queries: list[SplunkQuery] = field(default_factory=list)
    domains_targeted: list[str] = field(default_factory=list)

    def queries_for_stage(self, stage: int) -> list[SplunkQuery]:
        if stage == 1:
            return self.stage1_queries
        if stage == 2:
            return self.stage2_queries
        return self.stage3_queries

    def all_queries(self) -> list[SplunkQuery]:
        return self.stage1_queries + self.stage2_queries + self.stage3_queries

    def as_gap_queries(self, stage: Optional[int] = None) -> list[dict]:
        """Return gap-query-compatible dicts, optionally filtered by stage."""
        if stage is not None:
            return [q.to_gap_query() for q in self.queries_for_stage(stage)]
        return [q.to_gap_query() for q in self.all_queries()]


# ---------------------------------------------------------------------------
# Stage 1 — High-signal targeted indexes
# ---------------------------------------------------------------------------

def _build_stage1(incident_type: str, service: str, time_window: str) -> list[SplunkQuery]:
    """Application logs, container logs, DB logs, auth/cert logs, middleware."""
    queries: list[SplunkQuery] = []

    # Always: application error logs for the service
    queries.append(SplunkQuery(
        stage=1, worker="log_worker", action="get_error_logs",
        params={"service": service, "time_window": time_window},
        priority=0, signal_type="logs",
    ))

    # Always: golden signals (DT-style high-level health)
    queries.append(SplunkQuery(
        stage=1, worker="signal_worker", action="get_golden_signals",
        params={"service": service, "time_window": time_window},
        priority=1, signal_type="signals",
    ))

    # Always: K8s events for the service namespace
    queries.append(SplunkQuery(
        stage=1, worker="event_worker", action="get_k8s_events",
        params={"service": service, "time_window": time_window},
        priority=2, signal_type="events",
    ))

    # Incident-type specific high-signal searches
    itype = incident_type.lower()

    if itype in ("oomkill", "oom"):
        queries.append(SplunkQuery(
            stage=1, worker="log_worker", action="search_oom_logs",
            params={"service": service, "query": "OOMKilled OR out of memory OR memory.limit",
                    "time_window": time_window},
            priority=3, signal_type="logs",
        ))
        queries.append(SplunkQuery(
            stage=1, worker="metric_worker", action="query_memory_metrics",
            params={"service": service, "time_window": time_window},
            priority=4, signal_type="metrics",
        ))

    elif itype in ("timeout", "latency"):
        queries.append(SplunkQuery(
            stage=1, worker="log_worker", action="search_timeout_logs",
            params={"service": service, "query": "timeout OR deadline exceeded OR context canceled",
                    "time_window": time_window},
            priority=3, signal_type="logs",
        ))
        queries.append(SplunkQuery(
            stage=1, worker="metric_worker", action="query_response_time",
            params={"service": service, "time_window": time_window},
            priority=4, signal_type="metrics",
        ))

    elif itype in ("error_spike", "error"):
        queries.append(SplunkQuery(
            stage=1, worker="log_worker", action="search_error_logs",
            params={"service": service, "query": "ERROR OR FATAL OR exception",
                    "time_window": time_window},
            priority=3, signal_type="logs",
        ))
        queries.append(SplunkQuery(
            stage=1, worker="metric_worker", action="query_error_rate",
            params={"service": service, "time_window": time_window},
            priority=4, signal_type="metrics",
        ))

    elif itype == "saturation":
        queries.append(SplunkQuery(
            stage=1, worker="log_worker", action="search_saturation_logs",
            params={"service": service, "query": "queue.full OR thread.pool.exhausted OR connection.pool",
                    "time_window": time_window},
            priority=3, signal_type="logs",
        ))
        queries.append(SplunkQuery(
            stage=1, worker="metric_worker", action="query_saturation_metrics",
            params={"service": service, "time_window": time_window},
            priority=4, signal_type="metrics",
        ))

    else:
        # Generic: search all logs
        queries.append(SplunkQuery(
            stage=1, worker="log_worker", action="search_logs",
            params={"service": service, "query": "ERROR OR FATAL OR exception OR timeout",
                    "time_window": time_window},
            priority=3, signal_type="logs",
        ))

    # EKS / container logs — always high signal for K8s services
    queries.append(SplunkQuery(
        stage=1, worker="log_worker", action="search_logs",
        params={"service": service, "query": f"container OR pod OR namespace",
                "source": _src("eks_logs"), "time_window": time_window},
        priority=5, signal_type="logs",
    ))

    return sorted(queries, key=lambda q: q.priority)


# ---------------------------------------------------------------------------
# Stage 2 — Dependency-specific indexes
# ---------------------------------------------------------------------------

def _build_stage2(
    incident_type: str,
    service: str,
    time_window: str,
    domains: Optional[list[str]] = None,
) -> list[SplunkQuery]:
    """Kafka, PingFederate, CyberArk, DNS, AppViewX, Oracle, Ansible, process activity."""
    queries: list[SplunkQuery] = []
    domains = [d.upper() for d in (domains or [])]

    # CERTIFICATE domain
    if not domains or "CERTIFICATE" in domains:
        queries.append(SplunkQuery(
            stage=2, worker="log_worker", action="search_logs",
            params={"query": "ssl OR tls OR certificate OR x509 OR handshake",
                    "source": _src("cert_logs"), "time_window": time_window},
            priority=0, signal_type="logs",
        ))
        queries.append(SplunkQuery(
            stage=2, worker="log_worker", action="search_logs",
            params={"query": "AppViewX OR cert-renewal OR venafi", "source": _src("infra_logs"),
                    "time_window": time_window},
            priority=1, signal_type="logs",
        ))

    # IDENTITY domain — PingFederate
    if not domains or "IDENTITY" in domains:
        queries.append(SplunkQuery(
            stage=2, worker="log_worker", action="search_logs",
            params={"query": "PingFederate OR LDAP OR OAuth OR SAML OR SSO OR token expired",
                    "source": _src("auth_logs"), "time_window": time_window},
            priority=2, signal_type="logs",
        ))

    # CREDENTIAL domain — CyberArk / vault
    if not domains or "CREDENTIAL" in domains:
        queries.append(SplunkQuery(
            stage=2, worker="log_worker", action="search_logs",
            params={"query": "CyberArk OR CPM OR password rotation OR vault OR credential rotation",
                    "source": _src("cyberark_logs"), "time_window": time_window},
            priority=3, signal_type="logs",
        ))

    # DNS_AUTH domain — DNS / AppViewX
    if not domains or "DNS_AUTH" in domains:
        queries.append(SplunkQuery(
            stage=2, worker="log_worker", action="search_logs",
            params={"query": "NXDOMAIN OR DNS resolution OR getaddrinfo OR name resolution",
                    "source": _src("dns_logs"), "time_window": time_window},
            priority=4, signal_type="logs",
        ))

    # DB_AUTH domain — Oracle, PostgreSQL, MySQL connection pool
    if not domains or "DB_AUTH" in domains:
        queries.append(SplunkQuery(
            stage=2, worker="log_worker", action="search_logs",
            params={"query": "ORA- OR connection pool OR max connections OR DB authentication",
                    "source": _src("db_logs"), "time_window": time_window},
            priority=5, signal_type="logs",
        ))

    # Kafka / messaging (always useful for saturation/timeout)
    queries.append(SplunkQuery(
        stage=2, worker="log_worker", action="search_logs",
        params={"query": "kafka OR consumer.lag OR broker.unavailable OR topic.partition",
                "source": _src("kafka_logs"), "time_window": time_window},
        priority=6, signal_type="logs",
    ))

    # Ansible / infra automation changes
    queries.append(SplunkQuery(
        stage=2, worker="change_worker", action="get_config_changes",
        params={"filter": "ansible OR terraform OR automation OR runbook",
                "time_window": time_window},
        priority=7, signal_type="changes",
    ))

    # Process activity (unexpected process restarts, OOM kills at OS level)
    queries.append(SplunkQuery(
        stage=2, worker="log_worker", action="search_logs",
        params={"query": "OOMKill OR process.restart OR killed.signal OR SIGTERM OR SIGKILL",
                "source": _src("process_logs"), "time_window": time_window},
        priority=8, signal_type="logs",
    ))

    return sorted(queries, key=lambda q: q.priority)


# ---------------------------------------------------------------------------
# Stage 3 — Fallback broader correlation
# ---------------------------------------------------------------------------

def _build_stage3(service: str, time_window: str) -> list[SplunkQuery]:
    """Broad error correlation, network events, config drift, infra."""
    queries: list[SplunkQuery] = []

    # Broad application error correlation
    queries.append(SplunkQuery(
        stage=3, worker="log_worker", action="search_logs",
        params={"query": f"service={service} (ERROR OR WARN OR FATAL)",
                "time_window": time_window},
        priority=0, signal_type="logs",
    ))

    # Network events (packet loss, interface errors)
    queries.append(SplunkQuery(
        stage=3, worker="event_worker", action="get_network_events",
        params={"service": service, "time_window": time_window},
        priority=1, signal_type="events",
    ))

    # Config drift (recent config file changes)
    queries.append(SplunkQuery(
        stage=3, worker="change_worker", action="get_config_changes",
        params={"service": service, "time_window": time_window},
        priority=2, signal_type="changes",
    ))

    # Recent deployments (broader window)
    queries.append(SplunkQuery(
        stage=3, worker="change_worker", action="get_recent_deployments",
        params={"service": service, "time_window": time_window},
        priority=3, signal_type="changes",
    ))

    # APM traces for latency / dependency failures
    queries.append(SplunkQuery(
        stage=3, worker="signal_worker", action="get_apm_traces",
        params={"service": service, "time_window": time_window},
        priority=4, signal_type="signals",
    ))

    # CPU metrics (saturation, throttling)
    queries.append(SplunkQuery(
        stage=3, worker="metric_worker", action="query_cpu_metrics",
        params={"service": service, "time_window": time_window},
        priority=5, signal_type="metrics",
    ))

    return sorted(queries, key=lambda q: q.priority)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_plan(
    incident_type: str,
    service: str,
    time_window: str = "1h",
    domains: Optional[list[str]] = None,
    max_queries: int = _MAX_QUERIES,
) -> RetrievalPlan:
    """Build a staged retrieval plan for the given incident.

    Parameters
    ----------
    incident_type:  Incident type string (e.g. "oomkill", "timeout")
    service:        Target service name
    time_window:    Time window for queries (e.g. "1h", "2h", "30m")
    domains:        Optional list of dependency domains to target in Stage 2
                    (e.g. ["CERTIFICATE", "DB_AUTH"]). If None, all domains queried.
    max_queries:    Cap on total queries in the plan.
    """
    plan = RetrievalPlan(incident_type=incident_type, service=service)
    plan.domains_targeted = domains or []

    plan.stage1_queries = _build_stage1(incident_type, service, time_window)
    plan.stage2_queries = _build_stage2(incident_type, service, time_window, domains)
    plan.stage3_queries = _build_stage3(service, time_window)

    # Enforce total query cap
    total = len(plan.stage1_queries) + len(plan.stage2_queries) + len(plan.stage3_queries)
    if total > max_queries:
        # Trim from stage 3 first, then stage 2
        available = max_queries - len(plan.stage1_queries)
        if available <= 0:
            plan.stage1_queries = plan.stage1_queries[:max_queries]
            plan.stage2_queries = []
            plan.stage3_queries = []
        else:
            s2_cap = min(len(plan.stage2_queries), available // 2 + available % 2)
            s3_cap = min(len(plan.stage3_queries), available - s2_cap)
            plan.stage2_queries = plan.stage2_queries[:s2_cap]
            plan.stage3_queries = plan.stage3_queries[:s3_cap]

    logger.info(
        "Retrieval plan built: type=%s service=%s s1=%d s2=%d s3=%d domains=%s",
        incident_type, service,
        len(plan.stage1_queries), len(plan.stage2_queries), len(plan.stage3_queries),
        plan.domains_targeted,
    )
    return plan


def decide_stage(
    current_confidence: float,
    evidence_gathered: int,
    stage1_done: bool = False,
    stage2_done: bool = False,
) -> int:
    """Decide which retrieval stage to execute next.

    Returns 1, 2, or 3. Returns 0 if no more stages needed (decisive evidence).

    Parameters
    ----------
    current_confidence:  Current grounding confidence (0.0–1.0)
    evidence_gathered:   Number of evidence sources gathered so far
    stage1_done:         Whether Stage 1 has been executed
    stage2_done:         Whether Stage 2 has been executed
    """
    if not stage1_done:
        return 1

    # Stage 1 done — check if it was decisive
    if current_confidence >= _STAGE1_THRESHOLD and evidence_gathered >= 2:
        logger.debug(
            "Stage 1 decisive: conf=%.2f sources=%d — skipping stages 2 & 3",
            current_confidence, evidence_gathered,
        )
        return 0   # stop: decisive evidence

    if not stage2_done:
        return 2

    # Stage 2 done — check if it was decisive
    if current_confidence >= _STAGE2_THRESHOLD and evidence_gathered >= 3:
        logger.debug(
            "Stage 2 decisive: conf=%.2f sources=%d — skipping stage 3",
            current_confidence, evidence_gathered,
        )
        return 0

    return 3


def get_stage_queries(
    plan: RetrievalPlan,
    stage: int,
    budget_remaining: int = 999,
) -> list[dict]:
    """Get gap-query-compatible dicts for a specific stage, respecting budget.

    Parameters
    ----------
    plan:             The retrieval plan
    stage:            Stage to retrieve queries for (1, 2, or 3)
    budget_remaining: Maximum number of queries to return
    """
    queries = plan.queries_for_stage(stage)
    capped  = queries[:budget_remaining]
    return [q.to_gap_query() for q in capped]
