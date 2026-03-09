"""Intelligent tool selection module for SentinalAI.

Selects 3-5 relevant investigation steps per incident type,
rather than loading all 89 available tools.

Provides both:
- Legacy module-level functions (classify_incident, get_playbook)
- YAML-driven ToolSelector class for enriched catalog-aware selection
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# =========================================================================
# Hardcoded playbooks — the source of truth for the investigation engine
# =========================================================================

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
        {"worker": "itsm_worker", "action": "get_change_records", "label": "check_itsm_changes"},
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
        {"worker": "itsm_worker", "action": "get_change_records", "label": "check_itsm_changes"},
    ],
    "network": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "connection refused OR dns {service}", "label": "search_network_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "dns {service}", "label": "search_dns_logs"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
        {"worker": "itsm_worker", "action": "get_change_records", "label": "check_itsm_changes"},
    ],
    "cascading": [
        {"worker": "ops_worker", "action": "get_incident_by_id", "label": "fetch_incident"},
        {"worker": "log_worker", "action": "search_logs", "query_hint": "error cascade {service}", "label": "search_error_logs"},
        {"worker": "apm_worker", "action": "get_golden_signals", "label": "check_golden_signals"},
        {"worker": "metrics_worker", "action": "query_metrics", "label": "check_metrics"},
        {"worker": "log_worker", "action": "get_change_data", "label": "check_changes"},
        {"worker": "itsm_worker", "action": "get_change_records", "label": "check_itsm_changes"},
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
    "timeout": [
        "timeout", "timed out", "request timeout",
        "deadline", "gateway timeout", "504",
        "upstream timeout",
    ],
    "oomkill": [
        "oomkill", "oom", "out of memory", "killed",
        "memory pressure", "container killed", "cgroup",
        "memory limit", "heap exhaustion",
    ],
    "error_spike": [
        "error spike", "error rate", "exception", "500",
        "5xx", "502", "503", "internal server error",
        "exception rate", "unhandled exception", "panic", "crash",
    ],
    "latency": [
        "latency", "slow", "response time",
        "p95", "p99", "sla breach",
        "degraded performance", "high latency",
    ],
    "saturation": [
        "cpu", "saturation", "exhaustion", "disk full",
        "cpu throttle", "inode", "file descriptor",
        "thread exhaustion", "resource limit",
    ],
    "network": [
        "connectivity", "connection refused", "dns", "network",
        "econnrefused", "socket timeout", "network unreachable",
        "certificate", "tls", "ssl",
    ],
    "cascading": [
        "cascading", "cascade", "multiple services",
        "circuit breaker", "dependency failure",
        "upstream", "downstream", "chain",
    ],
    "missing_data": [
        "degraded", "missing data", "partial",
        "data gap", "stale data", "no metrics",
        "telemetry gap", "null values",
    ],
    "flapping": [
        "flapping", "intermittent", "sporadic",
        "oscillating", "bouncing", "unstable",
        "up-down", "recovering-failing",
    ],
    "silent_failure": [
        "throughput drop", "throughput", "stale", "silent",
        "zero traffic", "no requests",
        "queue backup", "backpressure",
    ],
}


# =========================================================================
# Valid incident types — canonical set used for validation everywhere
# =========================================================================

VALID_INCIDENT_TYPES: frozenset[str] = frozenset(INCIDENT_PLAYBOOKS.keys())

# =========================================================================
# Sentinel: indicates whether the last classify_incident call used LLM
# =========================================================================

_last_classification_used_llm: bool = False


def last_classification_used_llm() -> bool:
    """Return True if the most recent classify_incident() call used the LLM fallback."""
    return _last_classification_used_llm


# =========================================================================
# Legacy module-level functions (backward compatible)
# =========================================================================

def classify_incident(summary: str) -> str:
    """Classify an incident by its summary text.

    Returns one of the playbook keys, or ``"error_spike"`` as default.

    Classification strategy:
    1. Deterministic keyword matching (primary, fast, no cost)
    2. LLM-based classification (fallback, only when keywords default to
       error_spike AND ``LLM_ENABLED=true``)
    3. Safe default ``"error_spike"`` when both paths fail
    """
    global _last_classification_used_llm
    _last_classification_used_llm = False

    summary_lower = summary.lower()
    for incident_type, keywords in CLASSIFICATION_KEYWORDS.items():
        for kw in keywords:
            if kw in summary_lower:
                return incident_type

    # No keyword matched — try LLM fallback if enabled
    llm_enabled = os.environ.get("LLM_ENABLED", "false").lower() in ("true", "1", "yes")
    if llm_enabled:
        llm_result = classify_incident_llm(summary)
        if llm_result is not None:
            _last_classification_used_llm = True
            return llm_result

    return "error_spike"


def classify_incident_llm(summary: str) -> str | None:
    """Classify an incident using the LLM when keyword matching fails.

    This function is only called when keyword-based classification defaults
    to ``error_spike`` and ``LLM_ENABLED`` is true.

    Args:
        summary: The raw incident summary text.

    Returns:
        A valid incident type string, or ``None`` if the LLM fails or
        returns an invalid type (caller will fall back to ``"error_spike"``).
    """
    # Lazy import to avoid hard dependency on llm module at import time
    from supervisor.llm import converse  # noqa: PLC0415

    valid_types_str = ", ".join(sorted(VALID_INCIDENT_TYPES))
    system_prompt = (
        "You are an incident classification engine for an SRE platform. "
        "Given an incident summary, respond with EXACTLY ONE of these incident types:\n"
        f"{valid_types_str}\n\n"
        "Rules:\n"
        "- Respond with ONLY the incident type, nothing else.\n"
        "- No punctuation, no explanation, no extra text.\n"
        "- If uncertain, respond with: error_spike"
    )

    try:
        result = converse(
            system_prompt=system_prompt,
            user_message=summary,
            temperature=0.0,
            max_tokens=50,
        )

        if result.get("error"):
            logger.warning(
                "LLM classification failed for summary=%r: %s",
                summary[:120],
                result["error"],
            )
            return None

        raw_text = result.get("text", "").strip().lower()
        # Extract only the incident type (LLM might add whitespace/newlines)
        candidate = raw_text.split()[0] if raw_text else ""

        if candidate in VALID_INCIDENT_TYPES:
            logger.info(
                "LLM classified incident as %r for summary=%r model_id=%s",
                candidate,
                summary[:120],
                result.get("model_id", "unknown"),
            )
            return candidate

        logger.warning(
            "LLM returned invalid incident type %r for summary=%r; "
            "falling back to default",
            raw_text[:60],
            summary[:120],
        )
        return None

    except Exception as exc:
        logger.error(
            "LLM classification exception for summary=%r: %s",
            summary[:120],
            exc,
        )
        return None


def get_playbook(incident_type: str) -> list[dict]:
    """Return the investigation playbook for *incident_type*.

    G4.3: Logs a warning when an unknown incident type is received rather
    than silently defaulting. The default to error_spike is preserved for
    backward compatibility, but the warning enables audit trail detection
    of unexpected classification values.
    """
    if incident_type not in INCIDENT_PLAYBOOKS:
        logger.warning(
            "Unknown incident_type %r — defaulting to error_spike playbook",
            incident_type,
        )
    return INCIDENT_PLAYBOOKS.get(incident_type, INCIDENT_PLAYBOOKS["error_spike"])


# =========================================================================
# MCP tool name <-> worker name mapping
# =========================================================================

# Maps MCP server.tool_name to our internal worker name
MCP_TO_WORKER: dict[str, str] = {
    "moogsoft.get_incident_by_id": "ops_worker",
    "moogsoft.get_incidents": "ops_worker",
    "moogsoft.get_critical_incidents": "ops_worker",
    "moogsoft.get_alerts": "ops_worker",
    "moogsoft.get_historical_analysis": "knowledge_worker",
    "moogsoft.get_closed_incidents": "knowledge_worker",
    "splunk.search_oneshot": "log_worker",
    "splunk.search_export": "log_worker",
    "splunk.get_change_data": "log_worker",
    "splunk.app_change_data": "log_worker",
    "splunk.get_host_metrics": "metrics_worker",
    "splunk.get_health_status": "metrics_worker",
    "splunk.get_incident_data": "ops_worker",
    "sysdig.query_metrics": "metrics_worker",
    "sysdig.golden_signals": "apm_worker",
    "sysdig.get_events": "metrics_worker",
    "sysdig.discover_resources": "metrics_worker",
    "sysdig.environment_status": "metrics_worker",
    "signalfx.query_signalfx_metrics": "apm_worker",
    "signalfx.get_signalfx_active_incidents": "ops_worker",
    "dynatrace.get_problems": "apm_worker",
    "dynatrace.get_metrics": "apm_worker",
    "dynatrace.get_entities": "apm_worker",
    "dynatrace.get_events": "apm_worker",
    # ServiceNow (ITSM)
    "servicenow.get_ci_details": "itsm_worker",
    "servicenow.search_incidents": "itsm_worker",
    "servicenow.get_change_records": "itsm_worker",
    "servicenow.get_known_errors": "itsm_worker",
    # GitHub (DevOps)
    "github.get_recent_deployments": "devops_worker",
    "github.get_pr_details": "devops_worker",
    "github.get_commit_diff": "devops_worker",
    "github.get_workflow_runs": "devops_worker",
}

# Investigation phase budgets
PHASE_BUDGETS: dict[str, dict[str, Any]] = {
    "initial_context": {"max_calls": 2, "max_seconds": 5},
    "itsm_context": {"max_calls": 3, "max_seconds": 10},
    "evidence_gathering": {"max_calls": 5, "max_seconds": 30},
    "change_correlation": {"max_calls": 3, "max_seconds": 10},
    "devops_correlation": {"max_calls": 2, "max_seconds": 10},
    "historical_context": {"max_calls": 2, "max_seconds": 10},
}

# Rate limits per MCP server
RATE_LIMITS: dict[str, dict[str, Any]] = {
    "moogsoft": {"requests_per_minute": 60, "concurrent": 5},
    "splunk": {"requests_per_minute": 0, "concurrent": 10},  # 0 = unlimited
    "sysdig": {"requests_per_minute": 100, "concurrent": 10},
    "signalfx": {"requests_per_hour": 1000},
    "dynatrace": {"requests_per_minute": 100, "concurrent": 10},
    "servicenow": {"requests_per_minute": 60, "concurrent": 5},
    "github": {"requests_per_minute": 30, "concurrent": 3},
}


# =========================================================================
# YAML-driven ToolSelector class
# =========================================================================

class ToolSelector:
    """Intelligent tool selector backed by the MCP tool catalog YAML.

    Provides catalog-aware tool selection with phase budgets, rate limits,
    and token optimization. Falls back to hardcoded playbooks if the YAML
    catalog is unavailable.
    """

    def __init__(self, catalog_path: str | None = None):
        if catalog_path is None:
            catalog_path = str(
                Path(__file__).parent / "sentinalai_mcp_tool_catalog.yaml"
            )
        self.catalog: dict[str, Any] = {}
        self.catalog_loaded = False
        self._load_catalog(catalog_path)

    def _load_catalog(self, path: str) -> None:
        """Load and parse the YAML catalog, stripping markdown fences."""
        try:
            import yaml
        except ImportError:
            logger.debug("PyYAML not installed; using hardcoded playbooks only")
            return

        if not os.path.exists(path):
            logger.debug("Catalog file not found at %s", path)
            return

        try:
            with open(path, "r") as f:
                raw = f.read()
            # Strip markdown code fences that break YAML parsing
            cleaned = re.sub(r"^```\w*\s*$", "", raw, flags=re.MULTILINE)
            self.catalog = yaml.safe_load(cleaned) or {}
            self.catalog_loaded = True
            logger.debug("Loaded MCP tool catalog from %s", path)
        except Exception as exc:
            logger.warning("Failed to parse catalog %s: %s", path, exc)

    @property
    def selection_rules(self) -> dict:
        """Return selection_rules section from catalog."""
        return self.catalog.get("selection_rules", {})

    @property
    def playbooks(self) -> dict:
        """Return playbooks section from catalog."""
        return self.catalog.get("playbooks", {})

    def classify_incident_type(self, incident_summary: str) -> str:
        """Classify an incident from its summary text.

        Delegates to the module-level classify_incident for consistency.
        """
        return classify_incident(incident_summary)

    def select_tools_for_incident(
        self,
        incident_type: str,
        investigation_phase: str = "evidence_gathering",
    ) -> list[str]:
        """Return the list of MCP tool names for this incident type and phase.

        Combines required + optional tools from the YAML selection_rules,
        filtered by investigation phase. Falls back to deriving tool names
        from the hardcoded playbooks.
        """
        by_type = self.selection_rules.get("by_incident_type", {})
        type_rules = by_type.get(incident_type, {})

        if type_rules:
            required = type_rules.get("required_tools", [])
            optional = type_rules.get("optional_tools", [])
            all_tools = list(required) + list(optional)

            # Filter by phase if phase tools are defined
            by_phase = self.selection_rules.get("by_investigation_phase", {})
            phase_tools = by_phase.get(investigation_phase, {}).get("tools", [])
            if phase_tools:
                filtered = [t for t in all_tools if t in phase_tools]
                return filtered if filtered else all_tools
            return all_tools

        # Fallback: derive MCP tool names from hardcoded playbook
        playbook = get_playbook(incident_type)
        return self._playbook_to_mcp_tools(playbook)

    def get_investigation_playbook(self, incident_type: str) -> list[dict]:
        """Return the investigation playbook for this incident type.

        Returns the hardcoded playbook (always available) enriched with
        phase information from the YAML catalog when available.
        """
        return get_playbook(incident_type)

    def should_call_tool(
        self,
        tool_name: str,
        incident_type: str,
        investigation_phase: str = "evidence_gathering",
    ) -> bool:
        """Check whether a specific MCP tool should be called.

        Returns True if the tool is in the selected set for this
        incident type and phase.
        """
        selected = self.select_tools_for_incident(incident_type, investigation_phase)
        return tool_name in selected

    def get_phase_budget(self, phase: str) -> dict[str, Any]:
        """Return the call budget for an investigation phase."""
        by_phase = self.selection_rules.get("by_investigation_phase", {})
        phase_config = by_phase.get(phase, {})
        if phase_config:
            return {
                "max_calls": phase_config.get("max_calls", PHASE_BUDGETS.get(phase, {}).get("max_calls", 5)),
                "tools": phase_config.get("tools", []),
            }
        return PHASE_BUDGETS.get(phase, {"max_calls": 5, "max_seconds": 30})

    def get_rate_limit(self, server: str) -> dict[str, Any]:
        """Return rate limit info for an MCP server."""
        catalog_limits = self.catalog.get("rate_limits", {})
        return catalog_limits.get(server, RATE_LIMITS.get(server, {}))

    def get_token_savings_estimate(self) -> dict[str, Any]:
        """Return token savings estimate for intelligent selection."""
        return {
            "full_catalog_tokens": 8000,
            "selected_tools_tokens": 500,
            "savings_percent": 94,
            "strategy": "Load 3-5 tools per incident instead of all 89",
        }

    @staticmethod
    def _steps_for_phase(phase: str, playbook: list[dict]) -> list[dict]:
        """Filter playbook steps belonging to a given investigation phase."""
        if phase == "initial_context":
            return [s for s in playbook if s["action"] == "get_incident_by_id"]
        if phase == "itsm_context":
            return [
                s for s in playbook
                if s["worker"] == "itsm_worker" and s["action"] != "get_change_records"
            ]
        if phase == "evidence_gathering":
            return [
                s for s in playbook
                if s["action"] in ("search_logs", "get_golden_signals", "query_metrics", "get_events")
            ]
        if phase == "change_correlation":
            return [
                s for s in playbook
                if s["action"] in ("get_change_data", "get_change_records")
            ]
        if phase == "devops_correlation":
            return [s for s in playbook if s["worker"] == "devops_worker"]
        if phase == "historical_context":
            return [s for s in playbook if s["worker"] == "knowledge_worker"]
        return []

    def get_investigation_workflow(self, incident_type: str) -> list[dict]:
        """Return a phased investigation workflow for the incident type.

        Combines playbook steps with phase budgets for a complete workflow.
        """
        playbook = get_playbook(incident_type)
        phases = [
            "initial_context", "itsm_context", "evidence_gathering",
            "change_correlation", "devops_correlation", "historical_context",
        ]
        workflow = []

        for phase in phases:
            budget = self.get_phase_budget(phase)
            phase_steps = self._steps_for_phase(phase, playbook)

            if phase_steps:
                workflow.append({
                    "phase": phase,
                    "max_calls": budget.get("max_calls", 5),
                    "steps": phase_steps,
                })

        return workflow

    def map_tool_to_worker(self, mcp_tool: str) -> str:
        """Map an MCP tool name to our internal worker name."""
        return MCP_TO_WORKER.get(mcp_tool, "")

    def _playbook_to_mcp_tools(self, playbook: list[dict]) -> list[str]:
        """Derive MCP tool names from a hardcoded playbook."""
        worker_action_to_mcp = {
            ("ops_worker", "get_incident_by_id"): "moogsoft.get_incident_by_id",
            ("log_worker", "search_logs"): "splunk.search_oneshot",
            ("log_worker", "get_change_data"): "splunk.get_change_data",
            ("apm_worker", "get_golden_signals"): "sysdig.golden_signals",
            ("metrics_worker", "query_metrics"): "sysdig.query_metrics",
            ("metrics_worker", "get_events"): "sysdig.get_events",
            ("knowledge_worker", "search_similar"): "moogsoft.get_historical_analysis",
            ("itsm_worker", "get_ci_details"): "servicenow.get_ci_details",
            ("itsm_worker", "search_incidents"): "servicenow.search_incidents",
            ("itsm_worker", "get_change_records"): "servicenow.get_change_records",
            ("itsm_worker", "get_known_errors"): "servicenow.get_known_errors",
            ("devops_worker", "get_recent_deployments"): "github.get_recent_deployments",
            ("devops_worker", "get_pr_details"): "github.get_pr_details",
            ("devops_worker", "get_commit_diff"): "github.get_commit_diff",
            ("devops_worker", "get_workflow_runs"): "github.get_workflow_runs",
        }
        tools = []
        for step in playbook:
            key = (step["worker"], step["action"])
            mcp_name = worker_action_to_mcp.get(key, f"{step['worker']}.{step['action']}")
            if mcp_name not in tools:
                tools.append(mcp_name)
        return tools
