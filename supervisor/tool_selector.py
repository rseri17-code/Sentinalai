"""Intelligent tool selection module for SentinalAI.

Selects 3-5 relevant investigation steps per incident type,
rather than loading all 89 available tools.
"""

from __future__ import annotations

# Playbooks map an incident classification to the ordered list of
# (worker_name, action, param_builder) tuples the supervisor should execute.
# Each param_builder is a callable(incident_info) -> dict.

INCIDENT_PLAYBOOKS: dict[str, list[dict]] = {
    "timeout": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "timeout {service}", "label": "search_timeout_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "query_metrics", "metric_hint": "response_time_ms", "label": "check_latency_metrics"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "oomkill": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "OOMKilled {service}", "label": "search_oom_logs"},
        {"worker": "metrics_worker", "action": "query_metrics", "metric_hint": "memory_usage_bytes", "label": "check_memory_metrics"},
        {"worker": "metrics_worker", "action": "get_events", "label": "check_events"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "{service} heap OR memory", "label": "search_memory_logs"},
    ],
    "error_spike": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "error {service}", "label": "search_error_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
        {"worker": "metrics_worker", "action": "get_events", "label": "check_events"},
    ],
    "latency": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "latency OR slow {service}", "label": "search_latency_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "query_metrics", "metric_hint": "response_time_ms", "label": "check_latency_metrics"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "saturation": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "query_metrics", "metric_hint": "cpu_usage_percent", "label": "check_cpu_metrics"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "cpu OR thread {service}", "label": "search_cpu_logs"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "network": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "connection refused OR dns {service}", "label": "search_network_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "dns {service}", "label": "search_dns_logs"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "cascading": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "error cascade {service}", "label": "search_error_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "query_metrics", "label": "check_metrics"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "missing_data": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "error connection {service}", "label": "search_error_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "get_events", "label": "check_events"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "flapping": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "error {service}", "label": "search_error_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "query_metrics", "metric_hint": "db_connection_pool_active", "label": "check_pool_metrics"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
    ],
    "silent_failure": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "{service}", "label": "search_service_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "pipeline {service}", "label": "search_pipeline_logs"},
        {"worker": "metrics_worker", "action": "query_metrics", "label": "check_traffic_metrics"},
    ],
}

# Keywords used to classify an incident from its summary
CLASSIFICATION_KEYWORDS: dict[str, list[str]] = {
    "timeout": ["timeout", "timed out", "request timeout"],
    "oomkill": ["oomkill", "oom", "out of memory", "killed"],
    "error_spike": ["error spike", "error rate", "exception", "500"],
    "latency": ["latency", "slow", "response time"],
    "saturation": ["cpu", "saturation", "exhaustion", "disk full"],
    "network": ["connectivity", "connection refused", "dns", "network"],
    "cascading": ["cascading", "cascade", "multiple services"],
    "missing_data": ["degraded", "missing data", "partial"],
    "flapping": ["flapping", "intermittent", "sporadic"],
    "silent_failure": ["throughput drop", "throughput", "stale", "silent"],
}


def classify_incident(summary: str) -> str:
    """Classify an incident by its summary text.

    Returns one of the playbook keys, or ``"error_spike"`` as default.
    """
    summary_lower = summary.lower()
    for incident_type, keywords in CLASSIFICATION_KEYWORDS.items():
        for kw in keywords:
            if kw in summary_lower:
                return incident_type
    return "error_spike"


def get_playbook(incident_type: str) -> list[dict]:
    """Return the investigation playbook for *incident_type*."""
    return INCIDENT_PLAYBOOKS.get(incident_type, INCIDENT_PLAYBOOKS["error_spike"])
