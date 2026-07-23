"""Synthetic Enterprise Validation Platform — deterministic corpus builder.

Builds a curated corpus of enterprise investigation tasks spanning the real
enterprise tool sources (Splunk, Dynatrace, Sysdig, ServiceNow, CMDB,
Kubernetes, AWS, Network, Identity, Application, Database, Autosys,
ThousandEyes). It REUSES the existing engine-agnostic EIC benchmark
(``sentinel_core.eic.make_task`` / ``score_submission``) — no new evaluation
framework is introduced. Each corpus entry is:

  { "task": <canonical, content-addressed EIC task>,
    "expected": { owner, confidence_min/max, recommendation, sources, ... } }

The ``task`` carries the hidden ground truth (root cause, service, necessary /
decisive evidence, traps) graded by the EIC scorer; ``expected`` adds the
operator-facing ground truth (owner, confidence range, recommendation) for
operator-workflow validation.

This is SYNTHETIC VALIDATION DATA, clearly labelled — not production data, not
operator behavior, not benchmark results. It is deterministic: content-addressed
task hashes, sorted keys, no clock, no randomness. Rebuilding yields byte-
identical output.

Run:  python3 eval/enterprise/build_corpus.py   (writes eval/enterprise/corpus.json)
"""
from __future__ import annotations

import json
import os

from sentinel_core.eic import make_task

ENTERPRISE_CORPUS_SCHEMA_VERSION = 1

# The enterprise tool sources this corpus exercises (telemetry key prefixes).
TOOL_SOURCES = (
    "splunk", "dynatrace", "sysdig", "servicenow", "cmdb", "kubernetes",
    "aws", "network", "identity", "application", "database", "autosys",
    "thousandeyes",
)

# --- the curated enterprise incidents (synthetic; deterministic) -----------
# Each: EIC task inputs + operator-facing expectations. No clock, no randomness.
_INCIDENTS = [
    {
        "task_id": "ENT-DB-001", "category": "database",
        "difficulty": "competing_hypotheses",
        "incident": {"service": "payment-service", "severity": 1,
                     "summary": "payment 5xx spike + latency"},
        "telemetry": {
            "splunk_logs": {"errors": ["HikariPool timeout", "connection refused"]},
            "database_pool_metrics": {"active": 200, "max": 200, "wait_ms": 4200},
            "dynatrace_problems": {"problem": "response time degradation"},
            "cmdb_ci": {"service": "payment-service", "owner": "payments-team"},
            "servicenow_incident": {"number": "INC0012001", "priority": 1},
            "thousandeyes_probe": {"ok": True},
        },
        "ground_truth": {
            "root_cause": "database connection pool exhaustion in payment-service",
            "root_cause_keywords": ["connection pool", "exhaustion", "database"],
            "root_cause_service": "payment-service",
            "necessary_evidence": ["splunk_logs", "database_pool_metrics"],
            "decisive_evidence": ["database_pool_metrics"]},
        "traps": {"distractor_evidence": ["thousandeyes_probe"],
                  "false_hypotheses": ["network failure", "bad deployment"]},
        "expected": {"owner": "payments-team", "confidence_min": 70,
                     "confidence_max": 95,
                     "recommendation": "increase pool size / fix unclosed connections",
                     "incident_class": "saturation"},
    },
    {
        "task_id": "ENT-DEPLOY-001", "category": "deploy",
        "difficulty": "single_cause",
        "incident": {"service": "checkout-service", "severity": 1,
                     "summary": "error rate jump after release"},
        "telemetry": {
            "splunk_logs": {"errors": ["NullPointerException in v8.4"]},
            "servicenow_change": {"change_id": "CHG0044021", "release": "v8.4"},
            "dynatrace_problems": {"problem": "failure rate increase"},
            "cmdb_ci": {"service": "checkout-service", "owner": "commerce-team"},
            "kubernetes_events": {"rollout": "v8.4 complete"},
        },
        "ground_truth": {
            "root_cause": "regression introduced in checkout-service release v8.4",
            "root_cause_keywords": ["regression", "release", "v8.4", "deployment"],
            "root_cause_service": "checkout-service",
            "necessary_evidence": ["splunk_logs", "servicenow_change"],
            "decisive_evidence": ["servicenow_change"]},
        "traps": {"distractor_evidence": [],
                  "false_hypotheses": ["database exhaustion"]},
        "expected": {"owner": "commerce-team", "confidence_min": 70,
                     "confidence_max": 92,
                     "recommendation": "roll back checkout-service to v8.3",
                     "incident_class": "deploy"},
    },
    {
        "task_id": "ENT-K8S-001", "category": "kubernetes",
        "difficulty": "competing_hypotheses",
        "incident": {"service": "order-processor", "severity": 2,
                     "summary": "pods restarting"},
        "telemetry": {
            "kubernetes_events": {"reason": "OOMKilled", "count": 7},
            "sysdig_events": {"memory": "limit exceeded"},
            "splunk_logs": {"errors": ["heap OutOfMemory"]},
            "servicenow_change": {"change_id": "CHG0044099", "release": "v2.3.1"},
            "cmdb_ci": {"service": "order-processor", "owner": "fulfilment-team"},
        },
        "ground_truth": {
            "root_cause": "memory leak in order-processor causing OOMKill after v2.3.1",
            "root_cause_keywords": ["memory", "oomkill", "leak"],
            "root_cause_service": "order-processor",
            "necessary_evidence": ["kubernetes_events", "sysdig_events"],
            "decisive_evidence": ["kubernetes_events"]},
        "traps": {"distractor_evidence": [],
                  "false_hypotheses": ["node failure"]},
        "expected": {"owner": "fulfilment-team", "confidence_min": 65,
                     "confidence_max": 88,
                     "recommendation": "raise memory limit + patch leak in v2.3.2",
                     "incident_class": "oomkill"},
    },
    {
        "task_id": "ENT-NET-001", "category": "network",
        "difficulty": "competing_hypotheses",
        "incident": {"service": "api-gateway", "severity": 1,
                     "summary": "intermittent 504s"},
        "telemetry": {
            "thousandeyes_probe": {"packet_loss": 0.18, "path": "edge->core"},
            "network_path": {"hop_loss": True},
            "splunk_logs": {"errors": ["upstream timeout 504"]},
            "dynatrace_problems": {"problem": "gateway timeout"},
            "cmdb_ci": {"service": "api-gateway", "owner": "platform-team"},
            "database_pool_metrics": {"active": 20, "max": 200},
        },
        "ground_truth": {
            "root_cause": "network packet loss on edge->core path causing gateway 504s",
            "root_cause_keywords": ["packet loss", "network", "504", "timeout"],
            "root_cause_service": "api-gateway",
            "necessary_evidence": ["thousandeyes_probe", "network_path"],
            "decisive_evidence": ["thousandeyes_probe"]},
        "traps": {"distractor_evidence": ["database_pool_metrics"],
                  "false_hypotheses": ["db exhaustion"]},
        "expected": {"owner": "platform-team", "confidence_min": 65,
                     "confidence_max": 90,
                     "recommendation": "engage network team on edge->core path",
                     "incident_class": "network"},
    },
    {
        "task_id": "ENT-IDENTITY-001", "category": "identity",
        "difficulty": "single_cause",
        "incident": {"service": "auth-service", "severity": 1,
                     "summary": "login failures estate-wide"},
        "telemetry": {
            "identity_auth": {"error": "token signing key expired"},
            "splunk_logs": {"errors": ["JWT validation failed"]},
            "servicenow_incident": {"number": "INC0012044", "priority": 1},
            "cmdb_ci": {"service": "auth-service", "owner": "identity-team"},
        },
        "ground_truth": {
            "root_cause": "expired token signing key in auth-service",
            "root_cause_keywords": ["signing key", "expired", "token", "jwt"],
            "root_cause_service": "auth-service",
            "necessary_evidence": ["identity_auth", "splunk_logs"],
            "decisive_evidence": ["identity_auth"]},
        "traps": {"distractor_evidence": [], "false_hypotheses": ["network"]},
        "expected": {"owner": "identity-team", "confidence_min": 75,
                     "confidence_max": 95,
                     "recommendation": "rotate signing key + automate expiry alert",
                     "incident_class": "identity"},
    },
    {
        "task_id": "ENT-BATCH-001", "category": "batch",
        "difficulty": "competing_hypotheses",
        "incident": {"service": "settlement-batch", "severity": 2,
                     "summary": "nightly settlement delayed"},
        "telemetry": {
            "autosys_jobs": {"job": "SETTLE_EOD", "status": "TERMINATED",
                             "exit": 1},
            "splunk_logs": {"errors": ["upstream file not found"]},
            "aws_cloudwatch": {"s3_latency_ms": 40},
            "cmdb_ci": {"service": "settlement-batch", "owner": "treasury-team"},
        },
        "ground_truth": {
            "root_cause": "Autosys job SETTLE_EOD terminated on missing upstream file",
            "root_cause_keywords": ["autosys", "job", "terminated", "upstream file"],
            "root_cause_service": "settlement-batch",
            "necessary_evidence": ["autosys_jobs", "splunk_logs"],
            "decisive_evidence": ["autosys_jobs"]},
        "traps": {"distractor_evidence": ["aws_cloudwatch"],
                  "false_hypotheses": ["s3 latency"]},
        "expected": {"owner": "treasury-team", "confidence_min": 60,
                     "confidence_max": 85,
                     "recommendation": "add upstream-file dependency gate to SETTLE_EOD",
                     "incident_class": "batch"},
    },
    {
        "task_id": "ENT-AWS-001", "category": "cloud",
        "difficulty": "single_cause",
        "incident": {"service": "media-service", "severity": 2,
                     "summary": "upload failures"},
        "telemetry": {
            "aws_cloudwatch": {"s3_5xx": 120, "throttling": True},
            "splunk_logs": {"errors": ["SlowDown: reduce request rate"]},
            "cmdb_ci": {"service": "media-service", "owner": "media-team"},
        },
        "ground_truth": {
            "root_cause": "S3 request-rate throttling on media-service upload path",
            "root_cause_keywords": ["s3", "throttling", "request rate"],
            "root_cause_service": "media-service",
            "necessary_evidence": ["aws_cloudwatch", "splunk_logs"],
            "decisive_evidence": ["aws_cloudwatch"]},
        "traps": {"distractor_evidence": [], "false_hypotheses": ["disk full"]},
        "expected": {"owner": "media-team", "confidence_min": 68,
                     "confidence_max": 90,
                     "recommendation": "add S3 request-rate backoff + prefix sharding",
                     "incident_class": "saturation"},
    },
    {
        "task_id": "ENT-CASCADE-001", "category": "cascade",
        "difficulty": "competing_hypotheses",
        "incident": {"service": "storefront", "severity": 1,
                     "summary": "storefront degraded, multiple services alerting"},
        "telemetry": {
            "dynatrace_problems": {"problem": "cascading failure",
                                   "entry": "inventory-service"},
            "application_traces": {"slow_span": "inventory.reserve",
                                   "downstream": "cart"},
            "splunk_logs": {"errors": ["inventory timeout", "cart 503"]},
            "cmdb_ci": {"service": "inventory-service", "owner": "catalog-team"},
            "database_pool_metrics": {"active": 199, "max": 200},
            "thousandeyes_probe": {"ok": True},
        },
        "ground_truth": {
            "root_cause": "inventory-service DB saturation cascading to storefront",
            "root_cause_keywords": ["inventory", "saturation", "cascading"],
            "root_cause_service": "inventory-service",
            "necessary_evidence": ["dynatrace_problems", "database_pool_metrics"],
            "decisive_evidence": ["database_pool_metrics"]},
        "traps": {"distractor_evidence": ["thousandeyes_probe"],
                  "false_hypotheses": ["network", "storefront bug"]},
        "expected": {"owner": "catalog-team", "confidence_min": 62,
                     "confidence_max": 86,
                     "recommendation": "shed load + scale inventory-service DB pool",
                     "incident_class": "cascade"},
    },
]


def build_corpus() -> dict:
    entries = []
    for inc in _INCIDENTS:
        task = make_task(
            task_id=inc["task_id"], category=inc["category"],
            difficulty=inc["difficulty"], incident=inc["incident"],
            telemetry=inc["telemetry"], ground_truth=inc["ground_truth"],
            traps=inc.get("traps"))
        entries.append({"task": task, "expected": inc["expected"]})
    entries.sort(key=lambda e: e["task"]["task_id"])
    # tool sources actually exercised (evidence of coverage, not a claim)
    used_sources = sorted({s for s in TOOL_SOURCES for e in entries
                           for k in e["task"]["telemetry_keys"]
                           if k.startswith(s)})
    return {
        "schema_version": ENTERPRISE_CORPUS_SCHEMA_VERSION,
        "kind": "synthetic_enterprise_validation_corpus",
        "note": ("SYNTHETIC validation data — not production data, not operator "
                 "behavior, not benchmark results. Deterministic, content-"
                 "addressed via the EIC benchmark."),
        "tool_sources_declared": list(TOOL_SOURCES),
        "tool_sources_exercised": used_sources,
        "tasks": len(entries),
        "corpus": entries,
    }


def main() -> str:
    corpus = build_corpus()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.json")
    with open(out, "w") as f:
        json.dump(corpus, f, indent=2, sort_keys=True)
        f.write("\n")
    return out


if __name__ == "__main__":
    path = main()
    print("wrote", path)
