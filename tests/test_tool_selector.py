"""
Unit tests for the incident classification and playbook selection engine.

The tool_selector module is the decision brain of SentinalAI — misclassification
means the wrong investigation playbook runs, leading to garbage RCA output.
These tests exercise every classification path, edge case, and ambiguity.
"""

import pytest
from supervisor.tool_selector import (
    classify_incident,
    get_playbook,
    INCIDENT_PLAYBOOKS,
    CLASSIFICATION_KEYWORDS,
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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
