"""Cold-start seeder — prime ExperienceStore + ConfidenceCalibrator for new tenants.

Enterprise deployments start with an empty experience store, which degrades
quality for the first N investigations. This module generates realistic synthetic
labeled incident examples drawn from common SRE archetypes, seeds them into:

  1. ExperienceStore  — retrieval similarity matching benefits immediately
  2. ConfidenceCalibrator — calibration bins seeded with plausible accuracy data

Archetypes (8 canonical failure modes):
  - memory_leak          CPU/memory growth leading to OOM
  - deploy_regression    Quality drop following a deployment
  - cascading_dependency Upstream service failure propagating downstream
  - database_saturation  Connection pool exhaustion / slow queries
  - traffic_spike        Unexpected load exceeding capacity
  - certificate_expiry   TLS cert expired causing auth failures
  - dns_failure          DNS resolution breakdown causing connectivity loss
  - storage_pressure     Disk/inode exhaustion affecting writes

Each archetype generates SEED_EXAMPLES_PER_ARCHETYPE synthetic experiences
(default 3) with varied quality scores, confidence levels, and tag sets.

Usage:
    from supervisor.cold_start_seeder import seed_tenant

    seed_tenant(org_id="acme-corp")  # idempotent — skips if already seeded
    seed_tenant(org_id="acme-corp", force=True)  # re-seed even if already done
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.cold_start_seeder")

SEED_EXAMPLES_PER_ARCHETYPE = int(os.environ.get("SEED_EXAMPLES_PER_ARCHETYPE", "3"))
SEED_MARKER_KEY = "cold_start_seeded_v1"

_SEED_MARKER_DIR = os.environ.get(
    "SEED_MARKER_DIR",
    os.path.join(os.path.dirname(__file__), "..", "eval"),
)


# ---------------------------------------------------------------------------
# Archetypes
# ---------------------------------------------------------------------------

_ARCHETYPES: list[dict[str, Any]] = [
    {
        "incident_type": "memory_leak",
        "tags": ["memory", "oom", "heap", "gc"],
        "services": ["api-gateway", "user-service", "payment-processor"],
        "root_cause_templates": [
            "Unbounded cache growth in {service} caused heap exhaustion after {hours}h under sustained load.",
            "Memory leak in connection pool of {service}: objects not released after failed requests.",
            "Large response buffering in {service} middleware — per-request allocations not garbage-collected.",
        ],
        "fix_templates": [
            "Add LRU eviction policy to cache with max_entries=10000",
            "Fix connection pool release in finally block",
            "Switch to streaming response to avoid buffering large payloads",
        ],
        "quality_range": (0.70, 0.92),
        "confidence_range": (62, 85),
    },
    {
        "incident_type": "deploy_regression",
        "tags": ["deployment", "regression", "rollback", "release"],
        "services": ["checkout-service", "recommendation-engine", "search-api"],
        "root_cause_templates": [
            "Deploy of {service} v{version} introduced N+1 DB query in hot path, increasing p99 latency by {pct}%.",
            "Feature flag defaulting to True in {service} v{version} enabled incomplete feature for all users.",
            "Config change in {service} v{version} removed connection timeout, causing cascading slow queries.",
        ],
        "fix_templates": [
            "Rollback to previous version and add eager loading",
            "Rollback; fix feature flag default to False",
            "Rollback; restore timeout config and add regression test",
        ],
        "quality_range": (0.75, 0.95),
        "confidence_range": (70, 90),
    },
    {
        "incident_type": "cascading_dependency",
        "tags": ["dependency", "cascade", "circuit-breaker", "upstream"],
        "services": ["order-service", "inventory-service", "notification-service"],
        "root_cause_templates": [
            "{service} has no circuit breaker on upstream calls — timeouts propagated to all consumers.",
            "Missing retry budget in {service}: upstream degradation caused thread pool saturation.",
            "{service} synchronous calls to degraded dependency without fallback caused full availability loss.",
        ],
        "fix_templates": [
            "Add circuit breaker with 50% threshold and 30s reset window",
            "Implement exponential backoff with jitter and retry budget",
            "Add async fallback returning cached data when dependency is degraded",
        ],
        "quality_range": (0.68, 0.88),
        "confidence_range": (58, 82),
    },
    {
        "incident_type": "database_saturation",
        "tags": ["database", "connection-pool", "slow-query", "saturation"],
        "services": ["user-db", "orders-db", "analytics-db"],
        "root_cause_templates": [
            "Connection pool of {service} exhausted by long-running analytics queries blocking OLTP traffic.",
            "Missing index on {service} orders table caused full-table scans at {rps} RPS.",
            "Lock contention in {service}: bulk update holding row locks for >30s during peak traffic.",
        ],
        "fix_templates": [
            "Route analytics to read replica; increase connection pool for primary",
            "Add composite index on (user_id, created_at, status)",
            "Break bulk update into batches of 1000 with short sleep between",
        ],
        "quality_range": (0.72, 0.93),
        "confidence_range": (65, 88),
    },
    {
        "incident_type": "traffic_spike",
        "tags": ["traffic", "autoscaling", "capacity", "overload"],
        "services": ["api-gateway", "product-service", "cdn-edge"],
        "root_cause_templates": [
            "Marketing campaign drove 10x baseline traffic to {service} — autoscaling headroom insufficient.",
            "Bot traffic from scrapers overwhelmed {service} rate-limit not configured.",
            "Retry storm from downstream clients amplified {service} failure 50x during brief blip.",
        ],
        "fix_templates": [
            "Pre-scale before campaigns; increase autoscaling max replicas to 50",
            "Enable rate limiting at API gateway: 100 req/min per IP",
            "Add exponential backoff to all downstream clients",
        ],
        "quality_range": (0.65, 0.85),
        "confidence_range": (55, 78),
    },
    {
        "incident_type": "certificate_expiry",
        "tags": ["certificate", "tls", "ssl", "expiry", "auth"],
        "services": ["auth-service", "api-gateway", "internal-mtls"],
        "root_cause_templates": [
            "TLS cert for {service} expired at {time} — no automated renewal was configured.",
            "mTLS certificate rotation for {service} failed silently; clients rejected after grace period.",
            "Cert pinned in {service} mobile SDK not updated before expiry — all mobile clients rejected.",
        ],
        "fix_templates": [
            "Rotate cert immediately; enable cert-manager with 30-day renewal trigger",
            "Fix cert rotation pipeline; add monitoring on cert expiry <14 days",
            "Emergency cert release; implement cert transparency monitoring",
        ],
        "quality_range": (0.82, 0.97),
        "confidence_range": (78, 95),
    },
    {
        "incident_type": "dns_failure",
        "tags": ["dns", "resolution", "connectivity", "networking"],
        "services": ["service-mesh", "external-api", "database-discovery"],
        "root_cause_templates": [
            "DNS TTL misconfiguration in {service} caused stale records to persist after failover.",
            "CoreDNS pod in cluster restarted mid-request — {service} resolution failed for 45 seconds.",
            "Split-horizon DNS inconsistency left {service} resolving to old IP after blue-green switch.",
        ],
        "fix_templates": [
            "Reduce DNS TTL to 30s; add health check on DNS resolution",
            "Increase CoreDNS replicas to 3; add pod disruption budget",
            "Sync internal/external DNS zones; add verification step to deployment pipeline",
        ],
        "quality_range": (0.70, 0.90),
        "confidence_range": (62, 84),
    },
    {
        "incident_type": "storage_pressure",
        "tags": ["disk", "storage", "inode", "filesystem"],
        "services": ["log-aggregator", "metrics-store", "artifact-registry"],
        "root_cause_templates": [
            "Log rotation misconfiguration on {service} filled /var/log in {hours}h — writes blocked.",
            "Inode exhaustion on {service} from millions of small temp files — df showed 40% free but inode at 100%.",
            "Artifact registry for {service} not pruning old images — 2TB storage limit hit after CI surge.",
        ],
        "fix_templates": [
            "Fix logrotate config; add disk usage alert at 80%",
            "Clean temp files with cron; add inode monitoring alongside disk monitoring",
            "Enable automated image pruning policy: keep last 5 tags per repo",
        ],
        "quality_range": (0.75, 0.92),
        "confidence_range": (68, 88),
    },
]


# ---------------------------------------------------------------------------
# Seeding logic
# ---------------------------------------------------------------------------

def seed_tenant(org_id: str = "default", force: bool = False) -> dict[str, int]:
    """Seed the experience store for a new tenant.

    Args:
        org_id: Organisation identifier (used for logging and marker key).
        force:  Re-seed even if already seeded (default False).

    Returns:
        {"seeded": N, "skipped": M} counts.
    """
    marker_key = f"{SEED_MARKER_KEY}:{org_id}"
    marker_file = os.path.join(
        os.environ.get("SEED_MARKER_DIR", _SEED_MARKER_DIR),
        f".seed_{org_id.replace('/', '_')}.json",
    )

    if not force:
        try:
            if os.path.isfile(marker_file):
                logger.info("Cold-start seed already applied for org=%s — skipping", org_id)
                return {"seeded": 0, "skipped": len(_ARCHETYPES) * SEED_EXAMPLES_PER_ARCHETYPE}
        except Exception:
            pass

    seeded = 0
    skipped = 0

    for archetype in _ARCHETYPES:
        for i in range(SEED_EXAMPLES_PER_ARCHETYPE):
            try:
                _seed_example(archetype, i, org_id)
                seeded += 1
            except Exception as exc:
                logger.warning("Seed example failed archetype=%s i=%d: %s", archetype["incident_type"], i, exc)
                skipped += 1

    _seed_calibration_bins()

    try:
        import json as _json
        os.makedirs(os.path.dirname(marker_file), exist_ok=True)
        with open(marker_file, "w") as _f:
            _json.dump({
                "seeded_at": datetime.now(timezone.utc).isoformat(),
                "count": seeded,
                "force": force,
                "org_id": org_id,
            }, _f)
    except Exception:
        pass

    try:
        from database.ops_persistence import get_ops_store
        get_ops_store().set_state(marker_key, {
            "seeded_at": datetime.now(timezone.utc).isoformat(),
            "count": seeded,
        })
    except Exception:
        pass

    logger.info("Cold-start seed complete for org=%s: seeded=%d skipped=%d", org_id, seeded, skipped)
    return {"seeded": seeded, "skipped": skipped}


def _seed_example(archetype: dict[str, Any], variant: int, org_id: str) -> None:
    """Generate and store one synthetic experience for an archetype."""
    rng = random.Random(hashlib.md5(f"{archetype['incident_type']}:{variant}:{org_id}".encode()).hexdigest())

    service = rng.choice(archetype["services"])
    root_cause_tpl = rng.choice(archetype["root_cause_templates"])
    fix_tpl = rng.choice(archetype["fix_templates"])

    root_cause = root_cause_tpl.format(
        service=service,
        hours=rng.randint(2, 8),
        pct=rng.randint(15, 300),
        version=f"{rng.randint(1,5)}.{rng.randint(0,20)}.{rng.randint(0,5)}",
        rps=rng.randint(500, 5000),
        time=f"0{rng.randint(1,9)}:{rng.randint(0,5)}{rng.randint(0,9)} UTC",
    )

    q_lo, q_hi = archetype["quality_range"]
    c_lo, c_hi = archetype["confidence_range"]
    quality = round(rng.uniform(q_lo, q_hi), 3)
    confidence = rng.randint(c_lo, c_hi)

    incident_id = f"SEED-{archetype['incident_type'].upper()[:8]}-{variant+1:02d}"
    investigation_id = hashlib.md5(f"{incident_id}:{org_id}".encode()).hexdigest()[:12]

    experience = {
        "investigation_id": investigation_id,
        "incident_id": incident_id,
        "incident_type": archetype["incident_type"],
        "affected_service": service,
        "severity_label": rng.choice(["high", "critical", "medium"]),
        "root_cause": root_cause,
        "fix_applied": fix_tpl,
        "online_quality_score": quality,
        "confidence_calibrated": confidence,
        "tags": rng.sample(archetype["tags"], k=min(3, len(archetype["tags"]))),
        "evidence_count": rng.randint(4, 12),
        "rounds_run": rng.randint(1, 4),
        "elapsed_ms": rng.uniform(3000, 25000),
        "synthetic": True,
        "org_id": org_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        from supervisor.experience_store import ExperienceStore
        store = ExperienceStore()
        store.store(
            incident_type=archetype["incident_type"],
            service=service,
            root_cause=root_cause,
            fix_applied=fix_tpl,
            outcome_quality=quality,
            metadata=experience,
        )
    except Exception as exc:
        logger.debug("ExperienceStore seed skipped (%s): %s", archetype["incident_type"], exc)

    try:
        from database.ops_persistence import get_ops_store
        get_ops_store().persist_replay_meta({
            "investigation_id": investigation_id,
            "incident_id": incident_id,
            "initial_quality": round(quality - rng.uniform(0.05, 0.15), 3),
            "final_quality": quality,
            "rounds_run": experience["rounds_run"],
            "stuck": False,
            "confidence_raw": confidence + rng.randint(-5, 5),
            "confidence_calibrated": confidence,
            "experience_matches": rng.randint(0, 3),
            "learning_updated": True,
            "experience_stored": True,
            "elapsed_ms": experience["elapsed_ms"],
            "corrections": [],
            "narrative": root_cause[:120],
        })
    except Exception as exc:
        logger.debug("replay_meta seed skipped: %s", exc)


def _seed_calibration_bins() -> None:
    """Seed the confidence calibrator with plausible accuracy data."""
    try:
        from supervisor.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator()
        # Add synthetic observations spread across confidence deciles
        rng = random.Random(42)
        for confidence in range(10, 100, 10):
            accuracy = confidence / 100.0 + rng.uniform(-0.05, 0.05)
            accuracy = max(0.0, min(1.0, accuracy))
            for _ in range(5):
                cal.record_outcome(confidence=confidence, was_correct=rng.random() < accuracy)
    except Exception as exc:
        logger.debug("Calibration seed skipped: %s", exc)
