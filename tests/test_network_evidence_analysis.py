"""
Tests for _extract_network_evidence in supervisor/agent.py.

Verifies that ThousandEyes network evidence is correctly extracted and
that confidence_delta accumulation, capping, and owner determination work
as specified.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from supervisor.agent import SentinalAISupervisor


@pytest.fixture(autouse=True)
def _no_priming():
    """Suppress experience-replay priming so tests are hermetic."""
    with patch("supervisor.agent._retrieve_experiences", return_value=[]), \
         patch("supervisor.agent._get_tool_recommendations", return_value={}), \
         patch("supervisor.agent._kg_query_similar", return_value=[]):
        yield


@pytest.fixture
def supervisor():
    return SentinalAISupervisor()


# ---------------------------------------------------------------------------
# 1. High-confidence DNS evidence increases confidence delta
# ---------------------------------------------------------------------------

def test_high_confidence_dns_evidence_increases_confidence(supervisor):
    evidence = {
        "network_evidence": [
            {
                "availability": 0.0,
                "packet_loss": 100.0,
                "connect_time_ms": 0,
                "error_type": "dns_failure",
                "recommended_owner": "dns",
                "confidence": 0.9,
                "affected_scope": "all",
            }
        ],
        "network_correlation": [
            {
                "rule_id": "dns_001",
                "rule_name": "DNS resolution failure",
                "confidence_delta": 0.40,
                "owner": "dns",
                "rca_summary": "DNS resolution failed for all agents.",
            }
        ],
        "network_summary": "DNS resolution failure detected across all agents.",
    }

    result = supervisor._extract_network_evidence(evidence)

    assert result["total_confidence_delta"] == pytest.approx(0.40)
    assert result["top_owner"] == "dns"
    assert result["has_network_evidence"] is True


# ---------------------------------------------------------------------------
# 2. Empty network_evidence list → no evidence, zero delta
# ---------------------------------------------------------------------------

def test_empty_network_evidence_no_change(supervisor):
    evidence = {
        "network_evidence": [],
        "network_correlation": [],
        "network_summary": "",
    }

    result = supervisor._extract_network_evidence(evidence)

    assert result["has_network_evidence"] is False
    assert result["total_confidence_delta"] == pytest.approx(0.0)
    assert result["top_owner"] == "unknown"
    assert result["summary"] == ""


# ---------------------------------------------------------------------------
# 3. Multiple correlations each with delta=0.30 are capped at 0.40
# ---------------------------------------------------------------------------

def test_confidence_delta_capped_at_040(supervisor):
    evidence = {
        "network_evidence": [{"availability": 50.0}],
        "network_correlation": [
            {"rule_id": "r1", "confidence_delta": 0.30, "owner": "network"},
            {"rule_id": "r2", "confidence_delta": 0.30, "owner": "network"},
        ],
        "network_summary": "",
    }

    result = supervisor._extract_network_evidence(evidence)

    # 0.30 + 0.30 = 0.60 → capped at 0.40
    assert result["total_confidence_delta"] == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# 4. Most frequent owner across correlations is returned as top_owner
# ---------------------------------------------------------------------------

def test_extract_top_owner_from_correlations(supervisor):
    evidence = {
        "network_evidence": [{"availability": 80.0}],
        "network_correlation": [
            {"rule_id": "r1", "confidence_delta": 0.10, "owner": "cdn"},
            {"rule_id": "r2", "confidence_delta": 0.10, "owner": "network"},
            {"rule_id": "r3", "confidence_delta": 0.10, "owner": "network"},
        ],
        "network_summary": "",
    }

    result = supervisor._extract_network_evidence(evidence)

    assert result["top_owner"] == "network"


# ---------------------------------------------------------------------------
# 5. Missing network keys in evidence → safe zeroed result, no KeyError
# ---------------------------------------------------------------------------

def test_no_network_keys_in_evidence_safe(supervisor):
    evidence = {
        "some_other_key": {"logs": {"results": []}},
    }

    result = supervisor._extract_network_evidence(evidence)

    assert result["has_network_evidence"] is False
    assert result["total_confidence_delta"] == pytest.approx(0.0)
    assert result["top_owner"] == "unknown"
    assert result["summary"] == ""
    assert result["evidence_list"] == []
    assert result["correlation_list"] == []
