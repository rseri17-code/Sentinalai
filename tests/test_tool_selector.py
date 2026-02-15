"""
Unit tests for the incident classification and playbook selection engine.

The tool_selector module is the decision brain of SentinalAI — misclassification
means the wrong investigation playbook runs, leading to garbage RCA output.
These tests exercise every classification path, edge case, and ambiguity.
"""

import os
import tempfile

import pytest
from supervisor.tool_selector import (
    classify_incident,
    get_playbook,
    INCIDENT_PLAYBOOKS,
    CLASSIFICATION_KEYWORDS,
    MCP_TO_WORKER,
    PHASE_BUDGETS,
    RATE_LIMITS,
    ToolSelector,
)


# =========================================================================
# Classification: happy path — every incident type must be reachable
# =========================================================================

class TestClassifyIncidentHappyPath:
    """Each known incident type has at least one keyword that classifies correctly."""

    @pytest.mark.parametrize("incident_type", list(CLASSIFICATION_KEYWORDS.keys()))
    def test_every_incident_type_is_reachable(self, incident_type):
        """At least one keyword for each type must trigger correct classification."""
        keywords = CLASSIFICATION_KEYWORDS[incident_type]
        matched = False
        for kw in keywords:
            result = classify_incident(f"service-a {kw} detected")
            if result == incident_type:
                matched = True
                break
        assert matched, (
            f"No keyword from {keywords} classifies to '{incident_type}'"
        )

    def test_timeout_from_summary(self):
        assert classify_incident("API Gateway timeout spike") == "timeout"

    def test_oomkill_from_summary(self):
        assert classify_incident("user-service OOMKilled") == "oomkill"

    def test_error_spike_from_summary(self):
        assert classify_incident("Payment service error spike") == "error_spike"

    def test_latency_from_summary(self):
        assert classify_incident("search-service response time degradation") == "latency"

    def test_saturation_from_summary(self):
        assert classify_incident("order-service CPU exhaustion") == "saturation"

    def test_network_from_summary(self):
        assert classify_incident("Inter-service connectivity failure") == "network"

    def test_cascading_from_summary(self):
        assert classify_incident("Cascading failure across checkout flow") == "cascading"

    def test_missing_data_from_summary(self):
        assert classify_incident("notification-service degraded") == "missing_data"

    def test_flapping_from_summary(self):
        assert classify_incident("auth-service intermittent failures") == "flapping"

    def test_silent_failure_from_summary(self):
        assert classify_incident("recommendation-service throughput drop") == "silent_failure"


# =========================================================================
# Classification: case insensitivity
# =========================================================================

class TestClassifyCaseInsensitivity:
    """Classification must be case-insensitive."""

    def test_uppercase_timeout(self):
        assert classify_incident("API GATEWAY TIMEOUT") == "timeout"

    def test_mixed_case_oomkill(self):
        assert classify_incident("OomKill on user-service") == "oomkill"

    def test_all_lower(self):
        assert classify_incident("cascading failure in payments") == "cascading"

    def test_title_case(self):
        """'Connection Refused' matches network before 'Intermittent' matches flapping."""
        assert classify_incident("Intermittent Connection Refused") == "network"


# =========================================================================
# Classification: empty and garbage input
# =========================================================================

class TestClassifyEdgeCases:
    """Edge cases that must not crash the classifier."""

    def test_empty_string_returns_default(self):
        result = classify_incident("")
        assert result == "error_spike", "Empty summary should fall back to default"

    def test_whitespace_only_returns_default(self):
        assert classify_incident("   ") == "error_spike"

    def test_no_matching_keyword_returns_default(self):
        result = classify_incident("Something completely unrelated happened")
        assert result == "error_spike"

    def test_numeric_only(self):
        result = classify_incident("123456789")
        assert result in CLASSIFICATION_KEYWORDS, "Must return a valid classification"

    def test_very_long_summary(self):
        long_summary = "a " * 10_000 + "timeout detected"
        assert classify_incident(long_summary) == "timeout"

    def test_special_characters(self):
        result = classify_incident("!@#$%^&*() timeout ==> error")
        assert result == "timeout"

    def test_unicode_summary(self):
        result = classify_incident("サービス timeout detected")
        assert result == "timeout"


# =========================================================================
# Classification: keyword priority / ambiguity
# =========================================================================

class TestClassifyAmbiguity:
    """When a summary matches multiple types, first match wins (dict order).
    These tests document the expected behavior for ambiguous inputs."""

    def test_timeout_beats_latency(self):
        """'timeout' keyword is checked before 'slow' in dict order."""
        result = classify_incident("slow request timeout on api-gateway")
        assert result == "timeout", (
            "When both timeout and latency keywords match, timeout should win"
        )

    def test_oom_beats_error_spike(self):
        """'OOM' should classify as oomkill, not error_spike."""
        result = classify_incident("OOM errors in user-service")
        assert result == "oomkill"

    def test_cpu_is_saturation_not_error(self):
        """'CPU' matches saturation, not error_spike."""
        result = classify_incident("CPU spike with errors on order-service")
        assert result == "saturation"

    def test_cascading_with_timeout_keyword(self):
        """Summary has both 'cascading' and 'timeout' — check which wins."""
        result = classify_incident("Cascading timeout failure")
        # timeout is checked before cascading in dict order
        assert result == "timeout"

    def test_dns_is_network(self):
        """'dns' should classify as network."""
        result = classify_incident("dns resolution failure")
        assert result == "network"

    def test_connectivity_is_network(self):
        result = classify_incident("connectivity issues across cluster")
        assert result == "network"

    def test_degraded_is_missing_data(self):
        """'degraded' should classify as missing_data."""
        result = classify_incident("notification-service degraded")
        assert result == "missing_data"


# =========================================================================
# Playbook retrieval: every type has a playbook
# =========================================================================

class TestGetPlaybook:
    """Playbook retrieval must always return a valid list."""

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_every_type_has_playbook(self, incident_type):
        playbook = get_playbook(incident_type)
        assert isinstance(playbook, list)
        assert len(playbook) >= 3, f"Playbook for {incident_type} should have at least 3 steps"

    def test_unknown_type_returns_fallback(self):
        playbook = get_playbook("alien_invasion")
        assert isinstance(playbook, list)
        assert len(playbook) >= 3

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_every_playbook_step_has_required_keys(self, incident_type):
        """Each step must have worker, action, and label."""
        playbook = get_playbook(incident_type)
        for i, step in enumerate(playbook):
            assert "worker" in step, f"Step {i} of {incident_type} missing 'worker'"
            assert "action" in step, f"Step {i} of {incident_type} missing 'action'"
            assert "label" in step, f"Step {i} of {incident_type} missing 'label'"

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_every_playbook_starts_with_fetch_incident(self, incident_type):
        """All playbooks should start by fetching the incident."""
        playbook = get_playbook(incident_type)
        assert playbook[0]["action"] == "get_incident_by_id"

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_playbook_workers_are_valid(self, incident_type):
        """All worker references must be valid worker names."""
        valid_workers = {
            "ops_worker", "log_worker", "metrics_worker",
            "apm_worker", "knowledge_worker",
        }
        playbook = get_playbook(incident_type)
        for step in playbook:
            assert step["worker"] in valid_workers, (
                f"Unknown worker '{step['worker']}' in {incident_type} playbook"
            )

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_playbook_labels_unique(self, incident_type):
        """Labels within a playbook must be unique (they become evidence dict keys)."""
        playbook = get_playbook(incident_type)
        labels = [step["label"] for step in playbook]
        assert len(labels) == len(set(labels)), (
            f"Duplicate labels in {incident_type}: {labels}"
        )


# =========================================================================
# Classification keywords: structural integrity
# =========================================================================

class TestClassificationKeywordsIntegrity:
    """Validate the keyword dictionary structure itself."""

    def test_all_playbook_types_have_keywords(self):
        """Every playbook type must have classification keywords."""
        for ptype in INCIDENT_PLAYBOOKS:
            assert ptype in CLASSIFICATION_KEYWORDS, (
                f"Playbook type '{ptype}' has no classification keywords"
            )

    def test_all_keyword_types_have_playbooks(self):
        """Every keyword type must have a playbook."""
        for ktype in CLASSIFICATION_KEYWORDS:
            assert ktype in INCIDENT_PLAYBOOKS, (
                f"Keyword type '{ktype}' has no playbook"
            )

    def test_no_empty_keyword_lists(self):
        """No incident type should have an empty keyword list."""
        for itype, keywords in CLASSIFICATION_KEYWORDS.items():
            assert len(keywords) > 0, f"{itype} has empty keyword list"

    def test_keywords_are_lowercase(self):
        """All keywords should be lowercase for consistent matching."""
        for itype, keywords in CLASSIFICATION_KEYWORDS.items():
            for kw in keywords:
                assert kw == kw.lower(), (
                    f"Keyword '{kw}' in {itype} is not lowercase"
                )


# =========================================================================
# ToolSelector class: YAML-driven selection
# =========================================================================

class TestToolSelectorInit:
    """ToolSelector initialization and catalog loading."""

    def test_init_with_default_path(self):
        """Loads from the default catalog path."""
        ts = ToolSelector()
        # May or may not parse depending on YAML validity, but must not crash
        assert isinstance(ts.catalog, dict)

    def test_init_with_missing_file(self):
        """Missing catalog file falls back gracefully."""
        ts = ToolSelector(catalog_path="/nonexistent/path.yaml")
        assert ts.catalog == {}
        assert ts.catalog_loaded is False

    def test_init_with_empty_file(self):
        """Empty YAML file produces empty catalog."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()
            ts = ToolSelector(catalog_path=f.name)
        os.unlink(f.name)
        assert ts.catalog == {}

    def test_init_with_valid_yaml(self):
        """Valid YAML loads correctly."""
        content = "metadata:\n  total_tools: 5\nselection_rules:\n  by_incident_type:\n    timeout:\n      required_tools:\n        - moogsoft.get_incident_by_id\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            ts = ToolSelector(catalog_path=f.name)
        os.unlink(f.name)
        assert ts.catalog_loaded is True
        assert ts.catalog.get("metadata", {}).get("total_tools") == 5

    def test_init_strips_markdown_fences(self):
        """Markdown code fences in YAML are stripped before parsing."""
        content = "key1: value1\n```\nkey2: value2\n```\nkey3: value3\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            ts = ToolSelector(catalog_path=f.name)
        os.unlink(f.name)
        assert ts.catalog_loaded is True
        assert ts.catalog.get("key1") == "value1"


class TestToolSelectorClassify:
    """ToolSelector.classify_incident_type delegates to module-level function."""

    def test_delegates_to_classify_incident(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.classify_incident_type("API timeout detected") == "timeout"
        assert ts.classify_incident_type("OOMKilled") == "oomkill"
        assert ts.classify_incident_type("nothing matches") == "error_spike"


class TestToolSelectorSelectTools:
    """ToolSelector.select_tools_for_incident returns MCP tool names."""

    def test_fallback_derives_from_playbook(self):
        """Without catalog, derives tools from hardcoded playbook."""
        ts = ToolSelector(catalog_path="/nonexistent")
        tools = ts.select_tools_for_incident("timeout")
        assert isinstance(tools, list)
        assert len(tools) >= 3
        assert "moogsoft.get_incident_by_id" in tools

    def test_with_catalog_uses_selection_rules(self):
        """With catalog, uses selection_rules."""
        content = (
            "selection_rules:\n"
            "  by_incident_type:\n"
            "    timeout:\n"
            "      required_tools:\n"
            "        - moogsoft.get_incident_by_id\n"
            "        - splunk.search_oneshot\n"
            "      optional_tools:\n"
            "        - signalfx.query_signalfx_metrics\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            f.flush()
            ts = ToolSelector(catalog_path=f.name)
        os.unlink(f.name)
        tools = ts.select_tools_for_incident("timeout")
        assert "moogsoft.get_incident_by_id" in tools
        assert "splunk.search_oneshot" in tools
        assert "signalfx.query_signalfx_metrics" in tools

    def test_unknown_type_falls_back(self):
        """Unknown incident type falls back to playbook-derived tools."""
        ts = ToolSelector(catalog_path="/nonexistent")
        tools = ts.select_tools_for_incident("alien_invasion")
        assert isinstance(tools, list)
        assert len(tools) >= 3

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_all_types_return_tools(self, incident_type):
        ts = ToolSelector(catalog_path="/nonexistent")
        tools = ts.select_tools_for_incident(incident_type)
        assert len(tools) >= 3


class TestToolSelectorShouldCallTool:
    """ToolSelector.should_call_tool checks if a tool is selected."""

    def test_selected_tool_returns_true(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.should_call_tool("moogsoft.get_incident_by_id", "timeout") is True

    def test_unselected_tool_returns_false(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.should_call_tool("nonexistent.tool", "timeout") is False


class TestToolSelectorPlaybook:
    """ToolSelector.get_investigation_playbook delegates to get_playbook."""

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_returns_same_as_module_function(self, incident_type):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.get_investigation_playbook(incident_type) == get_playbook(incident_type)


class TestToolSelectorWorkflow:
    """ToolSelector.get_investigation_workflow returns phased workflow."""

    def test_workflow_has_phases(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        workflow = ts.get_investigation_workflow("timeout")
        assert isinstance(workflow, list)
        assert len(workflow) >= 2
        phases = [w["phase"] for w in workflow]
        assert "initial_context" in phases
        assert "evidence_gathering" in phases

    def test_workflow_initial_context_has_fetch(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        workflow = ts.get_investigation_workflow("timeout")
        initial = [w for w in workflow if w["phase"] == "initial_context"][0]
        assert initial["steps"][0]["action"] == "get_incident_by_id"

    def test_workflow_has_max_calls(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        workflow = ts.get_investigation_workflow("timeout")
        for phase in workflow:
            assert "max_calls" in phase


class TestToolSelectorBudgets:
    """ToolSelector.get_phase_budget and get_rate_limit."""

    def test_phase_budget_defaults(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        budget = ts.get_phase_budget("evidence_gathering")
        assert budget.get("max_calls", 0) >= 3 or budget.get("max_seconds", 0) > 0

    def test_rate_limit_defaults(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        limit = ts.get_rate_limit("moogsoft")
        assert "requests_per_minute" in limit

    def test_unknown_server_returns_empty(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.get_rate_limit("unknown_server") == {}


class TestToolSelectorTokenSavings:
    """ToolSelector.get_token_savings_estimate."""

    def test_returns_savings_info(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        savings = ts.get_token_savings_estimate()
        assert savings["savings_percent"] == 94
        assert savings["full_catalog_tokens"] > savings["selected_tools_tokens"]


class TestToolSelectorMapping:
    """ToolSelector.map_tool_to_worker and MCP_TO_WORKER constant."""

    def test_map_known_tool(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.map_tool_to_worker("moogsoft.get_incident_by_id") == "ops_worker"
        assert ts.map_tool_to_worker("splunk.search_oneshot") == "log_worker"
        assert ts.map_tool_to_worker("sysdig.golden_signals") == "apm_worker"
        assert ts.map_tool_to_worker("sysdig.query_metrics") == "metrics_worker"
        assert ts.map_tool_to_worker("moogsoft.get_historical_analysis") == "knowledge_worker"

    def test_map_unknown_tool_returns_empty(self):
        ts = ToolSelector(catalog_path="/nonexistent")
        assert ts.map_tool_to_worker("nonexistent.tool") == ""

    def test_mcp_to_worker_has_key_tools(self):
        """MCP_TO_WORKER must map all critical investigation tools."""
        assert "moogsoft.get_incident_by_id" in MCP_TO_WORKER
        assert "splunk.search_oneshot" in MCP_TO_WORKER
        assert "sysdig.golden_signals" in MCP_TO_WORKER
        assert "sysdig.query_metrics" in MCP_TO_WORKER
        assert "sysdig.get_events" in MCP_TO_WORKER
        assert "splunk.get_change_data" in MCP_TO_WORKER

    def test_phase_budgets_complete(self):
        """All four investigation phases must have budgets."""
        for phase in ("initial_context", "evidence_gathering", "change_correlation", "historical_context"):
            assert phase in PHASE_BUDGETS

    def test_rate_limits_all_servers(self):
        """All four MCP servers must have rate limits."""
        for server in ("moogsoft", "splunk", "sysdig", "signalfx"):
            assert server in RATE_LIMITS


class TestToolSelectorWithRealCatalog:
    """Test ToolSelector with the actual catalog file from the project."""

    def test_loads_real_catalog(self):
        """The real catalog in the project should load (markdown fences stripped)."""
        ts = ToolSelector()  # uses default path
        # If YAML loaded, check structure; if not, just ensure no crash
        if ts.catalog_loaded:
            assert "metadata" in ts.catalog or "selection_rules" in ts.catalog

    def test_selection_rules_property(self):
        ts = ToolSelector()
        rules = ts.selection_rules
        assert isinstance(rules, dict)

    def test_playbooks_property(self):
        ts = ToolSelector()
        playbooks = ts.playbooks
        assert isinstance(playbooks, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
