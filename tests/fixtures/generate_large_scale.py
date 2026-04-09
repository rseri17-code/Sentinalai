#!/usr/bin/env python3
"""Generate large-scale test fixtures for SentinalAI.

Produces 5 JSON fixture files:
  - servicenow_incidents_1000.json  (1000 incidents, 500 infra + 500 app)
  - moogsoft_incidents_1000.json    (1000 incidents, 500 infra + 500 app)
  - problem_records_1000.json       (1000 problem records)
  - splunk_logs_large.json          (10 indexes x 50 entries + per-incident)
  - sysdig_metrics_large.json       (30 services + infra + per-incident)
"""
import json
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)
OUTPUT_DIR = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# Data catalogs
# ─────────────────────────────────────────────────────────────────────────────

INFRA_SERVICES = [
    "k8s-node-1", "k8s-node-2", "k8s-node-3", "k8s-node-4", "k8s-node-5",
    "payment-db", "order-db", "user-db", "analytics-db", "audit-db",
    "redis-primary", "redis-replica", "memcached-1",
    "kafka-broker-1", "kafka-broker-2", "kafka-broker-3",
    "rabbitmq-1", "nginx-lb-1", "nginx-lb-2", "kube-dns",
    "istio-pilot", "nfs-server-1", "prometheus", "vault", "consul",
    "ceph-mon-1", "etcd-1", "haproxy-1", "elasticsearch-data-1",
]

APP_SERVICES = [
    "api-gateway", "auth-service", "payment-service", "order-service",
    "checkout-service", "user-service", "inventory-service", "search-service",
    "recommendation-service", "notification-service", "email-service",
    "shipping-service", "analytics-service", "reporting-service",
    "billing-service", "catalog-service", "review-service", "cart-service",
    "webhook-service", "config-service", "feature-flag-service",
    "data-pipeline", "etl-service", "message-processor", "batch-processor",
]

INFRA_INCIDENT_TYPES = [
    ("OOMKill",            "pod OOMKilled"),
    ("CrashLoopBackOff",   "pod in crash loop"),
    ("NodeNotReady",       "k8s node unhealthy"),
    ("DiskPressure",       "disk near full"),
    ("CPUSaturation",      "CPU at limit"),
    ("MemoryPressure",     "memory near limit"),
    ("NetworkConnectivity","network unreachable"),
    ("DNSFailure",         "DNS resolution failures"),
    ("CertExpiry",         "SSL cert expired/expiring"),
    ("PVCFailure",         "persistent volume claim unbound"),
    ("DatabaseDown",       "DB server unreachable"),
    ("DatabaseSlowQueries","DB query times elevated"),
    ("CacheDown",          "Redis/cache server down"),
    ("CacheOOM",           "cache memory exceeded"),
    ("KafkaConsumerLag",   "consumer group lagging"),
    ("KafkaBrokerDown",    "broker unavailable"),
    ("LoadBalancerDown",   "LB health checks failing"),
    ("IngressFailure",     "ingress controller error"),
    ("StorageIOSaturation","disk I/O saturated"),
    ("NFSMountFailure",    "NFS mount unresponsive"),
]

APP_INCIDENT_TYPES = [
    ("ErrorSpike",               "5xx error rate spike"),
    ("LatencySpike",             "response time elevated"),
    ("TimeoutCascade",           "cascading upstream timeouts"),
    ("ApplicationOOM",           "app memory leak + OOM"),
    ("ConnectionPoolExhaustion", "DB connections exhausted"),
    ("ThreadPoolExhaustion",     "worker threads all busy"),
    ("CircuitBreakerOpen",       "circuit breaker tripped"),
    ("SlowQueries",              "DB query time elevated"),
    ("Deadlock",                 "DB deadlock detected"),
    ("RateLimitExceeded",        "API rate limit hit"),
    ("CacheMissStorm",           "high cache miss rate"),
    ("DataPipelineFailure",      "ETL job failed"),
    ("AuthenticationFailure",    "auth errors"),
    ("AuthorizationFailure",     "permission denials"),
    ("FeatureFlagRegression",    "bad feature flag"),
    ("DeploymentRollout",        "deploy causing errors"),
    ("APIVersionMismatch",       "version incompatibility"),
    ("ConfigurationError",       "bad config deployed"),
    ("ExternalDependencyTimeout","3rd party timeout"),
    ("MessageProcessingFailure", "message queue failures"),
]

SPLUNK_INDEXES = [
    "production", "kubernetes", "database", "network", "security",
    "infrastructure", "apm", "audit", "pipeline", "middleware",
]

ASSIGNMENT_GROUPS = [
    "Platform Engineering", "SRE Team", "Database Team", "Network Team",
    "Security Team", "Application Engineering", "DevOps", "Data Engineering",
]

ASSIGNEES = [
    "Alice Chen", "Bob Martinez", "Carol Johnson", "David Kim", "Emma Wilson",
    "Frank Zhang", "Grace Patel", "Henry Brown", "Isabella Lee", "James Taylor",
]

MOOGSOFT_SEVERITIES = ["Critical", "Major", "High", "Warning", "Medium", "Minor", "Low", "Info"]
ALERT_PATTERNS = ["steady", "flapping", "spike", "gradual"]

INFRA_SUBCATEGORIES = ["Kubernetes", "Storage", "Network", "Database", "Cache", "Messaging", "Load Balancer", "DNS", "Certificate"]
APP_SUBCATEGORIES   = ["API", "Authentication", "Database", "Cache", "Messaging", "Pipeline", "Feature Flag", "Deployment", "External Dependency"]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

BASE_TIME = datetime(2024, 2, 15, 8, 0, 0)


def incident_time(idx: int) -> datetime:
    """Return the timestamp for incident at position idx (0-based)."""
    return BASE_TIME + timedelta(minutes=45 * idx)


def fmt_snow(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fmt_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def rand_in_range(lo: float, hi: float, decimals: int = 2) -> float:
    return round(random.uniform(lo, hi), decimals)


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow incidents
# ─────────────────────────────────────────────────────────────────────────────

def make_snow_incident(idx: int, category: str) -> dict:
    """Build one ServiceNow incident (idx 0-999)."""
    number  = f"INC{10001 + idx:07d}"
    created = incident_time(idx)
    updated = created + timedelta(minutes=random.randint(15, 120))

    priority = random.choice([1, 2, 3, 4])
    state    = random.choice(["1", "2", "3", "6", "7"])

    is_infra = category == "Infrastructure"
    service  = random.choice(INFRA_SERVICES if is_infra else APP_SERVICES)
    inc_type, inc_desc = random.choice(INFRA_INCIDENT_TYPES if is_infra else APP_INCIDENT_TYPES)
    subcat   = random.choice(INFRA_SUBCATEGORIES if is_infra else APP_SUBCATEGORIES)

    short_desc = f"{inc_type} on {service}: {inc_desc}"
    description = (
        f"Alert triggered: {inc_type} detected on service '{service}'. "
        f"Observed symptom: {inc_desc}. Immediate investigation required. "
        f"Impact assessed at priority {priority}."
    )

    record: dict = {
        "number":           number,
        "sys_id":           str(uuid.uuid4()),
        "short_description": short_desc,
        "description":      description,
        "cmdb_ci":          service,
        "priority":         priority,
        "state":            state,
        "sys_created_on":   fmt_snow(created),
        "sys_updated_on":   fmt_snow(updated),
        "assigned_to":      random.choice(ASSIGNEES),
        "assignment_group": random.choice(ASSIGNMENT_GROUPS),
        "category":         category,
        "subcategory":      subcat,
        "impact":           random.randint(1, 3),
        "urgency":          random.randint(1, 3),
        "resolution_notes": None,
        "resolved_at":      None,
        "root_cause":       None,
    }

    if state in ("6", "7"):
        resolved = updated + timedelta(minutes=random.randint(30, 240))
        record["resolution_notes"] = f"Resolved {inc_type} on {service} by {record['assigned_to']}."
        record["resolved_at"]      = fmt_snow(resolved)
        record["root_cause"]       = f"Root cause identified as {inc_desc} on {service}."

    return record


def generate_snow_incidents() -> list:
    incidents = []
    for i in range(500):
        incidents.append(make_snow_incident(i,       "Infrastructure"))
    for i in range(500):
        incidents.append(make_snow_incident(i + 500, "Application"))
    return incidents


# ─────────────────────────────────────────────────────────────────────────────
# Moogsoft incidents
# ─────────────────────────────────────────────────────────────────────────────

def make_moogsoft_incident(idx: int, category: str) -> dict:
    """Build one Moogsoft incident (idx 0-999)."""
    incident_id = f"INC{10001 + idx}"
    number      = f"INC{10001 + idx:07d}"
    start       = incident_time(idx)

    is_infra = category == "Infrastructure"
    service  = random.choice(INFRA_SERVICES if is_infra else APP_SERVICES)
    inc_type, inc_desc = random.choice(INFRA_INCIDENT_TYPES if is_infra else APP_INCIDENT_TYPES)
    severity = random.choice(MOOGSOFT_SEVERITIES)
    status   = random.choice(["New", "In Progress", "Resolved", "Closed"])

    tags = [random.choice(["production", "staging"]),
            service.split("-")[0],
            f"p{random.randint(1,4)}"]

    timeline = []
    for step in range(random.randint(2, 6)):
        ts = start + timedelta(minutes=step * random.randint(5, 20))
        timeline.append({
            "timestamp": fmt_iso(ts),
            "action":    random.choice(["detected", "acknowledged", "escalated", "investigating", "mitigated"]),
            "details":   f"Step {step + 1}: {inc_desc} on {service}",
        })

    anomaly_type = random.choice(["normal", "latency_spike", "error_spike", "throughput_drop", "saturation", "oomkill"])

    return {
        "incident_id":     incident_id,
        "number":          number,
        "summary":         f"{inc_type} on {service}: {inc_desc}",
        "severity":        severity,
        "status":          status,
        "affected_service": service,
        "start_time":      fmt_iso(start),
        "correlated_alerts": random.randint(1, 120),
        "tags":            tags,
        "timeline":        timeline,
        "alert_pattern":   random.choice(ALERT_PATTERNS),
        "source":          "moogsoft",
        "anomaly_type":    anomaly_type,
        "category":        category,
    }


def generate_moogsoft_incidents() -> list:
    incidents = []
    for i in range(500):
        incidents.append(make_moogsoft_incident(i,       "Infrastructure"))
    for i in range(500):
        incidents.append(make_moogsoft_incident(i + 500, "Application"))
    return incidents


# ─────────────────────────────────────────────────────────────────────────────
# Problem records
# ─────────────────────────────────────────────────────────────────────────────

def make_problem_record(idx: int, snow_incidents: list) -> dict:
    number   = f"PRB{10001 + idx:07d}"
    category = "Infrastructure" if idx < 500 else "Application"
    is_infra = category == "Infrastructure"
    service  = random.choice(INFRA_SERVICES if is_infra else APP_SERVICES)
    inc_type, inc_desc = random.choice(INFRA_INCIDENT_TYPES if is_infra else APP_INCIDENT_TYPES)
    state    = random.choice(["1", "2", "3", "4"])
    priority = random.randint(1, 4)
    known_error = random.choice([True, False])

    # Sample 1-3 related incident numbers from the first 1000 snow incidents
    sample_pool = [r["number"] for r in snow_incidents[:200]]
    related = random.sample(sample_pool, k=random.randint(1, 3))

    return {
        "number":           number,
        "sys_id":           str(uuid.uuid4()),
        "short_description": f"{inc_type} causing recurring issues on {service}",
        "description":      (
            f"Problem record tracking recurring {inc_type} on {service}. "
            f"Observed: {inc_desc}. Multiple related incidents filed."
        ),
        "state":            state,
        "priority":         priority,
        "cmdb_ci":          service,
        "assigned_to":      random.choice(ASSIGNEES),
        "assignment_group": random.choice(ASSIGNMENT_GROUPS),
        "category":         category,
        "root_cause":       f"Identified root cause: {inc_desc} on {service}.",
        "workaround":       f"Temporary workaround: restart {service} and monitor.",
        "related_incidents": related,
        "known_error":      known_error,
        "fix_notes":        f"Permanent fix: patch deployed to {service} to address {inc_type}." if state == "4" else "",
    }


def generate_problem_records(snow_incidents: list) -> list:
    return [make_problem_record(i, snow_incidents) for i in range(1000)]


# ─────────────────────────────────────────────────────────────────────────────
# Splunk logs
# ─────────────────────────────────────────────────────────────────────────────

HOSTS = [f"host-{i:03d}" for i in range(1, 21)]
LOG_LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
NAMESPACES = ["production", "staging"]
LOG_MESSAGES = [
    "Connection timeout after 30s",
    "Memory usage at 95% threshold",
    "Disk I/O wait exceeding 200ms",
    "Slow query detected: 8.3s",
    "Pod restarted due to OOMKill",
    "Health check failed for endpoint",
    "Certificate will expire in 7 days",
    "Consumer lag exceeded threshold: 50000 messages",
    "Circuit breaker opened for downstream service",
    "Rate limit exceeded: 429 responses increasing",
    "DNS resolution failed for internal service",
    "NFS mount became unresponsive",
    "Kafka broker unavailable",
    "Redis connection pool exhausted",
    "Deployment rolled back due to error spike",
]

SOURCE_TYPES = ["docker:log", "kube:events", "syslog", "json", "access_log"]
SOURCE_PATHS = [
    "/var/log/containers/{service}.log",
    "/var/log/pods/{service}/container.log",
    "/proc/kube/events",
    "/var/log/syslog",
    "/var/log/nginx/access.log",
]


def make_log_entry(idx: int, index: str, service: str = None, base_time: datetime = None) -> dict:
    if base_time is None:
        base_time = BASE_TIME + timedelta(minutes=random.randint(0, 60 * 24 * 30))
    if service is None:
        service = random.choice(INFRA_SERVICES + APP_SERVICES)

    host = random.choice(HOSTS)
    source_type = random.choice(SOURCE_TYPES)
    source_path = random.choice(SOURCE_PATHS).format(service=service)
    level = random.choice(LOG_LEVELS)
    namespace = random.choice(NAMESPACES)
    pod = f"{service}-{uuid.uuid4().hex[:5]}"
    message = random.choice(LOG_MESSAGES)

    raw = f'{fmt_iso(base_time)} {level} [{service}] {message} host={host} pod={pod}'

    return {
        "_time":      fmt_iso(base_time),
        "host":       host,
        "source":     source_path,
        "sourcetype": source_type,
        "index":      index,
        "_raw":       raw,
        "level":      level,
        "service":    service,
        "namespace":  namespace,
        "pod":        pod,
        "message":    message,
    }


def generate_splunk_logs(moogsoft_incidents: list) -> dict:
    # 10 indexes x 50 entries each
    indexes: dict = {}
    for index in SPLUNK_INDEXES:
        entries = []
        for j in range(50):
            service = random.choice(INFRA_SERVICES + APP_SERVICES)
            t = BASE_TIME + timedelta(minutes=j * 30 + random.randint(0, 20))
            entries.append(make_log_entry(j, index, service=service, base_time=t))
        indexes[index] = entries

    # incident_logs: first 50 Moogsoft incident IDs
    incident_logs: dict = {}
    for inc in moogsoft_incidents[:50]:
        inc_id = inc["incident_id"]
        service = inc["affected_service"]
        index = random.choice(SPLUNK_INDEXES)
        t = datetime.strptime(inc["start_time"], "%Y-%m-%dT%H:%M:%SZ")
        results = []
        for k in range(random.randint(3, 8)):
            entry_time = t + timedelta(minutes=k * 5)
            results.append(make_log_entry(k, index, service=service, base_time=entry_time))
        incident_logs[inc_id] = {
            "index":            index,
            "results":          results,
            "count":            len(results),
            "first_occurrence": fmt_iso(t),
        }

    return {"indexes": indexes, "incident_logs": incident_logs}


# ─────────────────────────────────────────────────────────────────────────────
# Sysdig metrics
# ─────────────────────────────────────────────────────────────────────────────

def make_golden_signals(anomaly: bool = False, anomaly_type: str = "normal") -> dict:
    base_p95 = rand_in_range(50, 300)
    multiplier = rand_in_range(2.0, 8.0) if anomaly and anomaly_type == "latency_spike" else 1.0
    p95 = round(base_p95 * multiplier, 2)
    p50 = round(p95 * rand_in_range(0.3, 0.6), 2)
    p99 = round(p95 * rand_in_range(1.2, 2.5), 2)

    base_rps = rand_in_range(50, 2000)
    rps = round(base_rps * (rand_in_range(0.1, 0.5) if anomaly and anomaly_type == "throughput_drop" else 1.0), 2)

    base_err = rand_in_range(0.001, 0.01)
    err_rate = round(base_err * (rand_in_range(10, 50) if anomaly and anomaly_type == "error_spike" else 1.0), 4)
    err_rate = min(err_rate, 1.0)

    cpu = rand_in_range(80, 100) if anomaly and anomaly_type == "saturation" else rand_in_range(10, 70)
    mem = rand_in_range(80, 100) if anomaly and anomaly_type in ("saturation", "oomkill") else rand_in_range(20, 75)

    return {
        "latency": {
            "p50":         p50,
            "p95":         p95,
            "p99":         p99,
            "baseline_p95": base_p95,
        },
        "traffic": {
            "rps":          rps,
            "baseline_rps": base_rps,
        },
        "errors": {
            "rate":          err_rate,
            "count":         int(err_rate * rps * 60),
            "baseline_rate": base_err,
        },
        "saturation": {
            "cpu":              round(cpu, 1),
            "memory":           round(mem, 1),
            "disk":             rand_in_range(20, 95),
            "network_rx_mbps":  rand_in_range(10, 500),
            "network_tx_mbps":  rand_in_range(10, 500),
        },
    }


def make_resource_metrics(service: str, base_time: datetime, count: int = 12) -> list:
    metrics = []
    metric_names = ["cpu_usage", "memory_usage", "disk_io", "network_rx", "network_tx", "request_count"]
    for name in metric_names:
        for step in range(count):
            t = base_time + timedelta(minutes=step * 5)
            metrics.append({
                "name":      name,
                "timestamp": fmt_iso(t),
                "value":     rand_in_range(0, 100),
            })
    return metrics


def make_k8s_events(service: str, base_time: datetime) -> list:
    event_types = ["Normal", "Warning"]
    severities  = ["Info", "Warning", "Critical"]
    k8s_messages = [
        f"Pod {service} started successfully",
        f"Liveness probe failed for {service}",
        f"Readiness probe failed for {service}",
        f"OOMKilled container in pod {service}",
        f"Failed to pull image for {service}",
        f"Node pressure eviction for {service}",
    ]
    events = []
    for _ in range(random.randint(2, 6)):
        t = base_time + timedelta(minutes=random.randint(0, 60))
        events.append({
            "type":      random.choice(event_types),
            "severity":  random.choice(severities),
            "message":   random.choice(k8s_messages),
            "timestamp": fmt_iso(t),
        })
    return events


def make_service_metrics(service: str, idx: int, anomaly: bool = False, anomaly_type: str = "normal") -> dict:
    base_time = BASE_TIME + timedelta(minutes=idx * 5)
    return {
        "service":          service,
        "namespace":        "production",
        "cluster":          "prod-k8s-01",
        "golden_signals":   make_golden_signals(anomaly=anomaly, anomaly_type=anomaly_type),
        "anomaly_detected": anomaly,
        "anomaly_type":     anomaly_type,
        "resource_metrics": make_resource_metrics(service, base_time),
        "k8s_events":       make_k8s_events(service, base_time),
    }


def generate_sysdig_metrics(moogsoft_incidents: list) -> dict:
    # 30 services: 25 app + first 5 infra to hit exactly 30
    svc_list = APP_SERVICES[:25] + INFRA_SERVICES[:5]  # 30 total
    services: dict = {}
    for i, svc in enumerate(svc_list):
        anomaly = random.random() < 0.3
        atype   = random.choice(["latency_spike", "error_spike", "throughput_drop", "saturation", "oomkill"]) if anomaly else "normal"
        services[svc] = make_service_metrics(svc, i, anomaly=anomaly, anomaly_type=atype)

    # Infrastructure: nodes + cluster summary
    nodes: dict = {}
    for node in INFRA_SERVICES[:5]:  # k8s-node-1..5
        nodes[node] = {
            "node":             node,
            "cluster":          "prod-k8s-01",
            "cpu_allocatable":  rand_in_range(60, 95),
            "memory_allocatable": rand_in_range(60, 90),
            "pod_count":        random.randint(10, 50),
            "conditions":       {
                "Ready":           "True",
                "DiskPressure":    "False",
                "MemoryPressure":  "False",
                "PIDPressure":     "False",
            },
            "resource_metrics": make_resource_metrics(node, BASE_TIME),
            "k8s_events":       make_k8s_events(node, BASE_TIME),
        }

    infrastructure = {
        "nodes":   nodes,
        "cluster": {
            "name":          "prod-k8s-01",
            "total_nodes":   5,
            "ready_nodes":   random.randint(4, 5),
            "total_pods":    random.randint(150, 300),
            "running_pods":  random.randint(140, 290),
            "pending_pods":  random.randint(0, 10),
            "failed_pods":   random.randint(0, 5),
        },
    }

    # incident_metrics: first 50 Moogsoft incident IDs
    incident_metrics: dict = {}
    for inc in moogsoft_incidents[:50]:
        inc_id  = inc["incident_id"]
        service = inc["affected_service"]
        anomaly_type = inc.get("anomaly_type", "normal")
        anomaly = anomaly_type != "normal"
        incident_metrics[inc_id] = make_service_metrics(
            service, 0, anomaly=anomaly, anomaly_type=anomaly_type
        )

    return {
        "services":         services,
        "infrastructure":   infrastructure,
        "incident_metrics": incident_metrics,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def generate_all():
    print("Generating ServiceNow incidents...")
    snow = generate_snow_incidents()
    out = OUTPUT_DIR / "servicenow_incidents_1000.json"
    out.write_text(json.dumps(snow, indent=2))
    print(f"  -> {out} ({len(snow)} records)")

    print("Generating Moogsoft incidents...")
    moog = generate_moogsoft_incidents()
    out = OUTPUT_DIR / "moogsoft_incidents_1000.json"
    out.write_text(json.dumps(moog, indent=2))
    print(f"  -> {out} ({len(moog)} records)")

    print("Generating problem records...")
    probs = generate_problem_records(snow)
    out = OUTPUT_DIR / "problem_records_1000.json"
    out.write_text(json.dumps(probs, indent=2))
    print(f"  -> {out} ({len(probs)} records)")

    print("Generating Splunk logs...")
    splunk = generate_splunk_logs(moog)
    out = OUTPUT_DIR / "splunk_logs_large.json"
    out.write_text(json.dumps(splunk, indent=2))
    index_count = len(splunk["indexes"])
    inc_log_count = len(splunk["incident_logs"])
    print(f"  -> {out} ({index_count} indexes, {inc_log_count} incident log groups)")

    print("Generating Sysdig metrics...")
    sysdig = generate_sysdig_metrics(moog)
    out = OUTPUT_DIR / "sysdig_metrics_large.json"
    out.write_text(json.dumps(sysdig, indent=2))
    svc_count = len(sysdig["services"])
    inc_met_count = len(sysdig["incident_metrics"])
    print(f"  -> {out} ({svc_count} services, {inc_met_count} incident metric groups)")

    print("\nAll fixture files generated successfully.")


if __name__ == "__main__":
    generate_all()
