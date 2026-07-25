"""EFIC — Enterprise Failure Intelligence Corpus (builder).

Knowledge, not framework. EFIC is the canonical, deterministic knowledge base of
realistic enterprise failure modes used to answer one question: *can SentinelAI
determine the correct root cause from the evidence its MCPs expose?*

It REUSES existing assets — the EIC task format (`sentinel_core.eic.make_task`,
so the EB-0 runner grades it unchanged) and the enterprise-corpus builder
pattern (`eval/enterprise`). Each corpus entry is:

  { "task":     <EIC-compatible task: incident, telemetry, hidden ground truth
                 + traps — the engine must EARN the answer>,
    "expected": <operator-facing: owner, confidence range, recommendation>,
    "efic":     <the enterprise scenario model: family, mode, business impact,
                 MCP utilization (required/optional/expected_empty/n_a),
                 reasoning contract, negative evidence, red herrings, replay
                 seed, difficulty, reasoning category> }

Every scenario is a DISTINCT reasoning problem (deduplicated by
family+mode+reasoning_category). Deterministic: content-addressed task hashes,
no clock, no randomness. This is a *foundational* set; the coverage model
(`coverage.json`) records exactly which taxonomy modes have scenarios and which
remain gaps — nothing is padded to inflate a count.

Run: python3 eval/efic/build_corpus.py
Writes: eval/efic/{taxonomy.json, corpus.json, coverage.json}
"""
from __future__ import annotations

import json
import os

from sentinel_core.eic import make_task

EFIC_SCHEMA_VERSION = 1

# All MCP sources a scenario may reference (utilization is declared per scenario).
MCPS = (
    "splunk", "dynatrace", "sysdig", "servicenow", "cmdb", "moogsoft",
    "thousandeyes", "aws_cloudwatch", "autosys", "identity", "network",
    "application", "database", "kubernetes", "route53_dns", "certificates",
)

# ---------------------------------------------------------------------------
# Canonical enterprise failure taxonomy: family -> failure modes.
# A mode is listed because it is a DISTINCT reasoning problem, not a symptom.
# ---------------------------------------------------------------------------
TAXONOMY: dict[str, list[str]] = {
    "kubernetes": ["oomkilled", "crashloopbackoff", "imagepullbackoff",
                   "node_pressure_eviction", "readiness_probe_failure"],
    "application_runtime": ["memory_leak", "thread_pool_exhaustion",
                            "gc_pause", "deadlock"],
    "database": ["connection_pool_exhaustion", "deadlock", "slow_query",
                 "replica_lag", "disk_full"],
    "messaging": ["kafka_consumer_lag", "rabbitmq_queue_backup",
                  "redis_eviction"],
    "networking": ["packet_loss", "network_partition", "mtu_blackhole"],
    "dns": ["resolution_failure", "stale_record"],
    "certificates": ["certificate_expiry", "chain_incomplete"],
    "identity": ["token_signing_key_expiry", "oauth_misconfig", "ldap_outage",
                 "iam_permission_denied"],
    "cloud_aws": ["s3_request_throttling", "regional_impairment",
                  "security_group_misconfig"],
    "api_gateway": ["gateway_5xx", "istio_mtls_failure", "rate_limit_saturation"],
    "deployment": ["regression", "config_drift", "bad_rollout"],
    "batch": ["autosys_dependency_failure", "job_timeout"],
    "storage": ["volume_full", "iops_throttle"],
    "observability": ["metrics_gap", "alert_storm"],
}


def _scn(*, sid, title, family, mode, service, severity, summary, root_cause,
         keywords, rc_service, necessary, decisive, distractors, false_hyps,
         telemetry, mcp, contributing, negative_evidence, red_herrings,
         considered, eliminated, conf, owner, recommendation, difficulty,
         reasoning_category, business_impact):
    """Expand a concise scenario spec into a corpus entry (task + expected + efic)."""
    task = make_task(
        task_id=sid, category=family, difficulty=difficulty,
        incident={"service": service, "severity": severity, "summary": summary},
        telemetry=telemetry,
        ground_truth={"root_cause": root_cause, "root_cause_keywords": keywords,
                      "root_cause_service": rc_service,
                      "necessary_evidence": necessary,
                      "decisive_evidence": decisive},
        traps={"distractor_evidence": distractors, "false_hypotheses": false_hyps})
    return {
        "task": task,
        "expected": {"owner": owner, "confidence_min": conf[0],
                     "confidence_max": conf[1], "recommendation": recommendation,
                     "incident_class": family},
        "efic": {
            "scenario_id": sid, "title": title, "failure_family": family,
            "failure_mode": mode, "business_impact": business_impact,
            "mcp_utilization": {m: mcp.get(m, "not_applicable") for m in MCPS},
            "contributing_factors": contributing,
            "negative_evidence": negative_evidence, "red_herrings": red_herrings,
            "hypotheses_considered": considered,
            "hypotheses_eliminated": eliminated,
            "expected_confidence_range": list(conf),
            "expected_owner": owner, "expected_recommendation": recommendation,
            "reasoning_category": reasoning_category, "difficulty": difficulty,
            "replay_seed": task["task_hash"],
        },
    }


# ---------------------------------------------------------------------------
# Foundational scenario set — one DISTINCT reasoning problem per high-value mode.
# ---------------------------------------------------------------------------
_SCENARIOS = [
    _scn(sid="EFIC-K8S-OOM-001", title="Payment pods OOMKilled after v8 rollout",
         family="kubernetes", mode="oomkilled", service="payment-service",
         severity=1, summary="payment pods restarting, 5xx rising",
         root_cause="memory leak in payment-service v8.2 causing OOMKill",
         keywords=["memory", "oomkill", "leak"], rc_service="payment-service",
         necessary=["kubernetes", "sysdig"], decisive=["kubernetes"],
         distractors=["thousandeyes"], false_hyps=["node failure", "network"],
         telemetry={"kubernetes": {"reason": "OOMKilled", "restarts": 9},
                    "sysdig": {"rss": "limit exceeded", "trend": "monotonic up"},
                    "servicenow": {"change": "CHG5001 v8.2", "when": "-2h"},
                    "thousandeyes": {"ok": True}},
         mcp={"kubernetes": "required", "sysdig": "required",
              "servicenow": "optional", "thousandeyes": "expected_empty",
              "splunk": "optional"},
         contributing=["heap sizing unchanged for new workload"],
         negative_evidence=["thousandeyes shows healthy network — rules out network"],
         red_herrings=["a concurrent DNS blip on an unrelated service"],
         considered=["memory leak", "node failure", "network"],
         eliminated=["node failure (other pods healthy)", "network (TE green)"],
         conf=(72, 92), owner="payments-team",
         recommendation="raise memory limit + patch leak in v8.3",
         difficulty="single_cause", reasoning_category="resource_saturation_temporal",
         business_impact="checkout failures for paying customers"),

    _scn(sid="EFIC-K8S-CLB-001", title="Auth pods CrashLoopBackOff on bad config",
         family="kubernetes", mode="crashloopbackoff", service="auth-service",
         severity=1, summary="auth pods crash looping",
         root_cause="invalid config map key crashes auth-service on startup",
         keywords=["config", "crashloop", "startup"], rc_service="auth-service",
         necessary=["kubernetes", "splunk"], decisive=["splunk"],
         distractors=["thousandeyes"], false_hyps=["image pull", "oom"],
         telemetry={"kubernetes": {"reason": "CrashLoopBackOff", "exit": 1},
                    "splunk": {"errors": ["FATAL missing config key OIDC_URL"]},
                    "servicenow": {"change": "CHG5010 configmap edit"}},
         mcp={"kubernetes": "required", "splunk": "required",
              "servicenow": "optional", "thousandeyes": "not_applicable"},
         contributing=["config change merged without validation"],
         negative_evidence=["image digest resolves — rules out image pull"],
         red_herrings=["OOM-looking restart count"],
         considered=["config error", "image pull", "oom"],
         eliminated=["image pull (digest OK)", "oom (no OOMKill reason)"],
         conf=(75, 93), owner="identity-team",
         recommendation="revert configmap; add startup config validation",
         difficulty="single_cause", reasoning_category="config_startup_failure",
         business_impact="estate-wide login failures"),

    _scn(sid="EFIC-DB-POOL-001", title="Checkout DB pool exhaustion under load",
         family="database", mode="connection_pool_exhaustion",
         service="checkout-service", severity=1,
         summary="checkout latency + 5xx spike",
         root_cause="database connection pool exhaustion from unclosed connections",
         keywords=["connection pool", "exhaustion", "database"],
         rc_service="checkout-service", necessary=["splunk", "database"],
         decisive=["database"], distractors=["thousandeyes"],
         false_hyps=["network", "bad deployment"],
         telemetry={"splunk": {"errors": ["HikariPool timeout"]},
                    "database": {"active": 200, "max": 200, "wait_ms": 4200},
                    "dynatrace": {"problem": "response time degradation"},
                    "thousandeyes": {"ok": True}},
         mcp={"splunk": "required", "database": "required",
              "dynatrace": "optional", "thousandeyes": "expected_empty",
              "servicenow": "optional"},
         contributing=["traffic surge exposed a connection leak"],
         negative_evidence=["no recent deploy — weakens 'bad deployment'"],
         red_herrings=["dynatrace flags latency without a cause"],
         considered=["pool exhaustion", "network", "bad deployment"],
         eliminated=["network (TE green)", "deployment (no change window)"],
         conf=(70, 90), owner="commerce-team",
         recommendation="fix connection leak; size pool to load",
         difficulty="competing_hypotheses", reasoning_category="resource_saturation_leak",
         business_impact="checkout degraded at peak"),

    _scn(sid="EFIC-DB-DEADLOCK-001", title="Order writes failing on DB deadlock",
         family="database", mode="deadlock", service="order-service",
         severity=2, summary="intermittent order write failures",
         root_cause="lock-order inversion causing database deadlocks in order-service",
         keywords=["deadlock", "lock", "database"], rc_service="order-service",
         necessary=["database", "splunk"], decisive=["database"],
         distractors=[], false_hyps=["pool exhaustion", "slow query"],
         telemetry={"database": {"deadlocks": 37, "victim": "order_txn"},
                    "splunk": {"errors": ["deadlock victim; transaction rolled back"]},
                    "dynatrace": {"problem": "failure rate spike"}},
         mcp={"database": "required", "splunk": "required",
              "dynatrace": "optional"},
         contributing=["new code path acquires locks in reverse order"],
         negative_evidence=["pool utilization normal — rules out pool exhaustion"],
         red_herrings=["a slow query on an unrelated report"],
         considered=["deadlock", "pool exhaustion", "slow query"],
         eliminated=["pool exhaustion (pool healthy)", "slow query (unrelated)"],
         conf=(68, 88), owner="commerce-team",
         recommendation="normalize lock acquisition order; add retry",
         difficulty="competing_hypotheses", reasoning_category="concurrency_correctness",
         business_impact="order write failures"),

    _scn(sid="EFIC-KAFKA-LAG-001", title="Notification lag from stuck Kafka consumer",
         family="messaging", mode="kafka_consumer_lag",
         service="notification-service", severity=2,
         summary="notifications delayed by hours",
         root_cause="notification-service consumer stuck on a poison message, lag growing",
         keywords=["kafka", "consumer", "lag", "poison"],
         rc_service="notification-service", necessary=["splunk", "dynatrace"],
         decisive=["splunk"], distractors=["thousandeyes"],
         false_hyps=["broker outage", "network"],
         telemetry={"splunk": {"errors": ["deserialization error; consumer paused"]},
                    "dynatrace": {"consumer_lag": 1_200_000, "trend": "up"},
                    "thousandeyes": {"ok": True}},
         mcp={"splunk": "required", "dynatrace": "required",
              "thousandeyes": "expected_empty"},
         contributing=["no dead-letter handling for malformed messages"],
         negative_evidence=["broker metrics healthy — rules out broker outage"],
         red_herrings=["one broker restarted hours earlier, unrelated"],
         considered=["poison message", "broker outage", "network"],
         eliminated=["broker outage (brokers healthy)", "network (TE green)"],
         conf=(66, 86), owner="platform-team",
         recommendation="add dead-letter queue; skip+alert on poison message",
         difficulty="competing_hypotheses", reasoning_category="stuck_processing",
         business_impact="delayed customer notifications"),

    _scn(sid="EFIC-NET-LOSS-001", title="Intermittent 504s from edge packet loss",
         family="networking", mode="packet_loss", service="api-gateway",
         severity=1, summary="intermittent gateway 504s",
         root_cause="packet loss on edge->core path causing gateway upstream timeouts",
         keywords=["packet loss", "network", "504", "timeout"],
         rc_service="api-gateway", necessary=["thousandeyes", "network"],
         decisive=["thousandeyes"], distractors=["database"],
         false_hyps=["db exhaustion", "bad deploy"],
         telemetry={"thousandeyes": {"packet_loss": 0.18, "path": "edge->core"},
                    "network": {"hop_loss": True},
                    "splunk": {"errors": ["upstream timeout 504"]},
                    "database": {"active": 20, "max": 200}},
         mcp={"thousandeyes": "required", "network": "required",
              "splunk": "optional", "database": "expected_empty"},
         contributing=["a flapping optic on an edge link"],
         negative_evidence=["DB pool underutilized — rules out db exhaustion"],
         red_herrings=["healthy-looking DB metrics invite a wrong hypothesis"],
         considered=["network loss", "db exhaustion", "bad deploy"],
         eliminated=["db exhaustion (pool idle)", "deploy (no change)"],
         conf=(64, 88), owner="network-team",
         recommendation="engage network on edge->core; fail over the link",
         difficulty="competing_hypotheses", reasoning_category="infra_network_path",
         business_impact="intermittent API failures across services"),

    _scn(sid="EFIC-DNS-001", title="App-wide failures from stale DNS record",
         family="dns", mode="stale_record", service="catalog-service",
         severity=1, summary="catalog cannot reach its datastore",
         root_cause="stale Route53 record points catalog-service to a decommissioned endpoint",
         keywords=["dns", "stale", "route53", "record"], rc_service="catalog-service",
         necessary=["route53_dns", "splunk"], decisive=["route53_dns"],
         distractors=["dynatrace"], false_hyps=["datastore outage", "cert"],
         telemetry={"route53_dns": {"record": "db.catalog", "points_to": "old-endpoint"},
                    "splunk": {"errors": ["connection refused to old-endpoint"]},
                    "dynatrace": {"problem": "dependency unavailable"}},
         mcp={"route53_dns": "required", "splunk": "required",
              "dynatrace": "optional", "certificates": "expected_empty"},
         contributing=["migration left the old record in place"],
         negative_evidence=["new datastore is healthy — rules out datastore outage"],
         red_herrings=["an expired cert on a sibling service"],
         considered=["stale DNS", "datastore outage", "certificate"],
         eliminated=["datastore outage (new endpoint healthy)", "cert (unrelated)"],
         conf=(70, 90), owner="platform-team",
         recommendation="update Route53 record; add post-migration DNS check",
         difficulty="competing_hypotheses", reasoning_category="misconfig_reference",
         business_impact="catalog unavailable"),

    _scn(sid="EFIC-CERT-001", title="TLS handshake failures on cert expiry",
         family="certificates", mode="certificate_expiry", service="billing-gateway",
         severity=1, summary="clients cannot establish TLS to billing",
         root_cause="expired TLS certificate on billing-gateway",
         keywords=["certificate", "expired", "tls", "handshake"],
         rc_service="billing-gateway", necessary=["certificates", "splunk"],
         decisive=["certificates"], distractors=["thousandeyes"],
         false_hyps=["network", "lb misconfig"],
         telemetry={"certificates": {"cn": "billing-gateway", "status": "expired"},
                    "splunk": {"errors": ["tls handshake failure: certificate expired"]},
                    "thousandeyes": {"tls_error": True}},
         mcp={"certificates": "required", "splunk": "required",
              "thousandeyes": "optional"},
         contributing=["cert auto-renewal job silently failed"],
         negative_evidence=["routing/LB config unchanged — rules out lb misconfig"],
         red_herrings=["a recent LB config touch that is unrelated"],
         considered=["cert expiry", "network", "lb misconfig"],
         eliminated=["network (reachable, TLS-only failure)", "lb (config stable)"],
         conf=(78, 95), owner="platform-team",
         recommendation="rotate certificate; fix renewal automation + alert",
         difficulty="single_cause", reasoning_category="lifecycle_expiry",
         business_impact="billing clients cannot connect"),

    _scn(sid="EFIC-IDENTITY-001", title="Estate login failure from signing-key expiry",
         family="identity", mode="token_signing_key_expiry", service="auth-service",
         severity=1, summary="JWT validation failing estate-wide",
         root_cause="expired token signing key in auth-service",
         keywords=["signing key", "expired", "token", "jwt"],
         rc_service="auth-service", necessary=["identity", "splunk"],
         decisive=["identity"], distractors=[], false_hyps=["ldap outage", "network"],
         telemetry={"identity": {"error": "signing key expired", "kid": "k-2024"},
                    "splunk": {"errors": ["JWT signature validation failed"]},
                    "servicenow": {"incident": "INC7 priority 1"}},
         mcp={"identity": "required", "splunk": "required",
              "servicenow": "optional"},
         contributing=["key rotation calendar lapsed"],
         negative_evidence=["LDAP directory reachable — rules out LDAP outage"],
         red_herrings=["a transient LDAP latency blip"],
         considered=["signing key expiry", "ldap outage", "network"],
         eliminated=["ldap outage (directory healthy)", "network (auth reachable)"],
         conf=(76, 95), owner="identity-team",
         recommendation="rotate signing key; automate expiry alerting",
         difficulty="single_cause", reasoning_category="lifecycle_expiry",
         business_impact="estate-wide authentication outage"),

    _scn(sid="EFIC-IAM-001", title="Batch failures from revoked IAM permission",
         family="identity", mode="iam_permission_denied", service="reporting-batch",
         severity=2, summary="nightly reporting job fails to read S3",
         root_cause="IAM policy change revoked reporting-batch S3 read permission",
         keywords=["iam", "permission", "denied", "s3"], rc_service="reporting-batch",
         necessary=["identity", "aws_cloudwatch"], decisive=["identity"],
         distractors=["thousandeyes"], false_hyps=["s3 outage", "network"],
         telemetry={"identity": {"policy_change": "CHG8 remove s3:GetObject"},
                    "aws_cloudwatch": {"s3_403": 240},
                    "splunk": {"errors": ["AccessDenied: not authorized s3:GetObject"]},
                    "thousandeyes": {"ok": True}},
         mcp={"identity": "required", "aws_cloudwatch": "required",
              "splunk": "optional", "thousandeyes": "expected_empty"},
         contributing=["a least-privilege cleanup over-pruned the policy"],
         negative_evidence=["S3 service health green — rules out S3 outage"],
         red_herrings=["S3 latency metric noise"],
         considered=["iam revocation", "s3 outage", "network"],
         eliminated=["s3 outage (health green)", "network (TE green)"],
         conf=(72, 92), owner="data-platform-team",
         recommendation="restore s3:GetObject on the batch role; add policy review",
         difficulty="competing_hypotheses", reasoning_category="authorization_change",
         business_impact="delayed nightly reporting"),

    _scn(sid="EFIC-AWS-S3-001", title="Upload failures from S3 request throttling",
         family="cloud_aws", mode="s3_request_throttling", service="media-service",
         severity=2, summary="media uploads failing intermittently",
         root_cause="S3 request-rate throttling on the media upload prefix",
         keywords=["s3", "throttling", "request rate", "slowdown"],
         rc_service="media-service", necessary=["aws_cloudwatch", "splunk"],
         decisive=["aws_cloudwatch"], distractors=[], false_hyps=["disk full", "network"],
         telemetry={"aws_cloudwatch": {"s3_503_slowdown": 130, "throttled": True},
                    "splunk": {"errors": ["SlowDown: reduce request rate"]}},
         mcp={"aws_cloudwatch": "required", "splunk": "required"},
         contributing=["hot key prefix; no request-rate sharding"],
         negative_evidence=["node disk usage normal — rules out disk full"],
         red_herrings=["a disk-usage alert on a sibling host"],
         considered=["s3 throttling", "disk full", "network"],
         eliminated=["disk full (disk normal)", "network (S3-specific errors)"],
         conf=(68, 90), owner="media-team",
         recommendation="shard S3 key prefixes; add request-rate backoff",
         difficulty="competing_hypotheses", reasoning_category="cloud_service_limit",
         business_impact="media upload failures"),

    _scn(sid="EFIC-DEPLOY-001", title="Error spike from checkout release regression",
         family="deployment", mode="regression", service="checkout-service",
         severity=1, summary="error rate jumped right after release",
         root_cause="null-pointer regression introduced in checkout-service v8.4",
         keywords=["regression", "release", "v8.4", "deployment"],
         rc_service="checkout-service", necessary=["splunk", "servicenow"],
         decisive=["servicenow"], distractors=[], false_hyps=["db exhaustion", "network"],
         telemetry={"splunk": {"errors": ["NullPointerException in v8.4"]},
                    "servicenow": {"change": "CHG9 v8.4 release", "when": "-10m"},
                    "dynatrace": {"problem": "failure rate increase"}},
         mcp={"splunk": "required", "servicenow": "required",
              "dynatrace": "optional"},
         contributing=["insufficient pre-prod coverage for the changed path"],
         negative_evidence=["DB pool healthy — rules out db exhaustion"],
         red_herrings=["a routine DB failover minutes earlier"],
         considered=["release regression", "db exhaustion", "network"],
         eliminated=["db exhaustion (pool healthy)", "network (internal error)"],
         conf=(74, 92), owner="commerce-team",
         recommendation="roll back checkout-service to v8.3",
         difficulty="single_cause", reasoning_category="change_correlation_temporal",
         business_impact="checkout error spike"),

    _scn(sid="EFIC-CONFIG-DRIFT-001", title="Partial outage from config drift after scale-out",
         family="deployment", mode="config_drift", service="search-service",
         severity=2, summary="a subset of search nodes return errors",
         root_cause="new search-service nodes came up with drifted (old) config",
         keywords=["config", "drift", "inconsistent", "nodes"],
         rc_service="search-service", necessary=["splunk", "cmdb"],
         decisive=["cmdb"], distractors=["thousandeyes"], false_hyps=["bad deploy", "network"],
         telemetry={"splunk": {"errors": ["only some nodes: invalid index config"]},
                    "cmdb": {"config_version": {"nodeA": "v3", "nodeB": "v2"}},
                    "thousandeyes": {"ok": True}},
         mcp={"splunk": "required", "cmdb": "required",
              "thousandeyes": "expected_empty"},
         contributing=["autoscaler used a stale launch template"],
         negative_evidence=["only new nodes affected — weakens 'bad deploy' (all would fail)"],
         red_herrings=["a recent deploy that actually succeeded"],
         considered=["config drift", "bad deploy", "network"],
         eliminated=["bad deploy (old nodes healthy)", "network (node-local errors)"],
         conf=(64, 86), owner="search-team",
         recommendation="reconcile launch template; enforce config parity check",
         difficulty="competing_hypotheses", reasoning_category="partial_fleet_inconsistency",
         business_impact="degraded search for a fraction of traffic"),

    _scn(sid="EFIC-BATCH-AUTOSYS-001", title="Settlement delayed by Autosys dependency failure",
         family="batch", mode="autosys_dependency_failure", service="settlement-batch",
         severity=2, summary="nightly settlement did not run",
         root_cause="Autosys job SETTLE_EOD terminated on a missing upstream file dependency",
         keywords=["autosys", "job", "terminated", "upstream file"],
         rc_service="settlement-batch", necessary=["autosys", "splunk"],
         decisive=["autosys"], distractors=["aws_cloudwatch"], false_hyps=["s3 latency", "db"],
         telemetry={"autosys": {"job": "SETTLE_EOD", "status": "TERMINATED", "exit": 1},
                    "splunk": {"errors": ["upstream file not found"]},
                    "aws_cloudwatch": {"s3_latency_ms": 40}},
         mcp={"autosys": "required", "splunk": "required",
              "aws_cloudwatch": "expected_empty"},
         contributing=["upstream extract job slipped its SLA"],
         negative_evidence=["S3 latency normal — rules out s3 slowness"],
         red_herrings=["a benign S3 latency metric"],
         considered=["autosys dependency", "s3 latency", "db"],
         eliminated=["s3 latency (normal)", "db (job never reached DB stage)"],
         conf=(62, 85), owner="treasury-team",
         recommendation="add upstream-file dependency gate + SLA alert to SETTLE_EOD",
         difficulty="competing_hypotheses", reasoning_category="batch_dependency_chain",
         business_impact="delayed financial settlement"),

    _scn(sid="EFIC-CASCADE-001", title="Storefront degraded by inventory DB saturation cascade",
         family="database", mode="connection_pool_exhaustion", service="storefront",
         severity=1, summary="storefront degraded, multiple services alerting",
         root_cause="inventory-service DB saturation cascading to storefront",
         keywords=["inventory", "saturation", "cascading"], rc_service="inventory-service",
         necessary=["dynatrace", "database"], decisive=["database"],
         distractors=["thousandeyes"], false_hyps=["network", "storefront bug"],
         telemetry={"dynatrace": {"problem": "cascading failure", "entry": "inventory-service"},
                    "database": {"active": 199, "max": 200},
                    "application": {"slow_span": "inventory.reserve", "downstream": "cart"},
                    "splunk": {"errors": ["inventory timeout", "cart 503"]},
                    "thousandeyes": {"ok": True}},
         mcp={"dynatrace": "required", "database": "required",
              "application": "optional", "splunk": "optional",
              "thousandeyes": "expected_empty"},
         contributing=["no bulkhead between inventory and storefront"],
         negative_evidence=["storefront code unchanged — weakens 'storefront bug'"],
         red_herrings=["storefront is where the symptom surfaces, not the cause"],
         considered=["inventory saturation", "network", "storefront bug"],
         eliminated=["network (TE green)", "storefront bug (no change; upstream slow)"],
         conf=(60, 86), owner="catalog-team",
         recommendation="shed load + scale inventory DB pool; add bulkhead",
         difficulty="competing_hypotheses", reasoning_category="cascade_localization",
         business_impact="storefront degraded for all shoppers"),

    _scn(sid="EFIC-ISTIO-MTLS-001", title="Service mesh 503s from mTLS cert rotation gap",
         family="api_gateway", mode="istio_mtls_failure", service="orders-mesh",
         severity=1, summary="east-west 503s after mesh upgrade",
         root_cause="istio mTLS failure: sidecar cert not rotated after CA change",
         keywords=["istio", "mtls", "sidecar", "certificate"], rc_service="orders-mesh",
         necessary=["kubernetes", "splunk"], decisive=["splunk"],
         distractors=["thousandeyes"], false_hyps=["network", "app bug"],
         telemetry={"kubernetes": {"event": "istio-proxy cert reload failed"},
                    "splunk": {"errors": ["upstream connect error; TLS handshake"]},
                    "dynatrace": {"problem": "east-west failure rate"},
                    "thousandeyes": {"ok": True}},
         mcp={"kubernetes": "required", "splunk": "required",
              "dynatrace": "optional", "thousandeyes": "expected_empty"},
         contributing=["mesh CA rotated without sidecar restart"],
         negative_evidence=["north-south (TE) healthy — isolates to east-west mesh"],
         red_herrings=["healthy external probes suggest 'not a real outage'"],
         considered=["mtls failure", "network", "app bug"],
         eliminated=["network (TE green)", "app bug (TLS-layer errors)"],
         conf=(66, 88), owner="platform-team",
         recommendation="restart sidecars to reload certs; automate on CA rotation",
         difficulty="competing_hypotheses", reasoning_category="mesh_security_rotation",
         business_impact="internal order flow failing"),
]


def _coverage(entries) -> dict:
    fams = {}
    for e in entries:
        ef = e["efic"]
        fams.setdefault(ef["failure_family"], set()).add(ef["failure_mode"])
    mcp_required = sorted({m for e in entries
                           for m, u in e["efic"]["mcp_utilization"].items()
                           if u == "required"})
    reasoning = sorted({e["efic"]["reasoning_category"] for e in entries})
    modes_covered = {m for ms in fams.values() for m in ms}
    taxonomy_modes = {f"{fam}:{m}" for fam, ms in TAXONOMY.items() for m in ms}
    covered_modes = {f"{e['efic']['failure_family']}:{e['efic']['failure_mode']}"
                     for e in entries}
    return {
        "failure_families_total": len(TAXONOMY),
        "failure_families_covered": sorted(fams),
        "failure_modes_total": len(taxonomy_modes),
        "failure_modes_covered": sorted(covered_modes),
        "failure_modes_gaps": sorted(taxonomy_modes - covered_modes),
        "mcp_required_coverage": mcp_required,
        "reasoning_categories": reasoning,
        "scenarios_with_negative_evidence": sum(
            1 for e in entries if e["efic"]["negative_evidence"]),
        "scenarios_with_red_herrings": sum(
            1 for e in entries if e["efic"]["red_herrings"]),
        "scenarios": len(entries),
    }


def build_corpus() -> dict:
    entries = sorted(_SCENARIOS, key=lambda e: e["task"]["task_id"])
    return {
        "schema_version": EFIC_SCHEMA_VERSION,
        "kind": "enterprise_failure_intelligence_corpus",
        "note": ("Synthetic, deterministic enterprise failure scenarios — the "
                 "engine must EARN the root cause; ground truth + traps are the "
                 "hidden answer key. Reuses the EIC task format (EB-0 runnable)."),
        "taxonomy": {k: sorted(v) for k, v in sorted(TAXONOMY.items())},
        "coverage": _coverage(entries),
        "corpus": entries,
    }


def main() -> str:
    corpus = build_corpus()
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "corpus.json"), "w") as f:
        json.dump(corpus, f, indent=2, sort_keys=True); f.write("\n")
    with open(os.path.join(here, "taxonomy.json"), "w") as f:
        json.dump({"schema_version": EFIC_SCHEMA_VERSION,
                   "taxonomy": {k: sorted(v) for k, v in sorted(TAXONOMY.items())}},
                  f, indent=2, sort_keys=True); f.write("\n")
    with open(os.path.join(here, "coverage.json"), "w") as f:
        json.dump(corpus["coverage"], f, indent=2, sort_keys=True); f.write("\n")
    return here


if __name__ == "__main__":
    p = main()
    c = build_corpus()
    print("scenarios:", c["coverage"]["scenarios"],
          "| families covered:", len(c["coverage"]["failure_families_covered"]),
          "of", c["coverage"]["failure_families_total"])
    print("mode gaps:", len(c["coverage"]["failure_modes_gaps"]))
