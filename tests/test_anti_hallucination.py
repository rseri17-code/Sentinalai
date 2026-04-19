"""Tests for anti-hallucination enforcement in supervisor.evidence_citation."""
from __future__ import annotations


from supervisor.evidence_citation import annotate_citations, _enforce_anti_hallucination


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOOD_EVIDENCE = {
    "logs": {"logs": [
        {"_raw": "connection pool exhausted max connections reached 1024"},
        {"_raw": "error acquiring database connection after 30s timeout"},
    ]},
    "metrics": {"signals": {"latency_ms": 5200, "error_rate": 0.087}},
    "apm_data": {"errors": [{"message": "pool exhausted connection refused"}]},
}

SPARSE_EVIDENCE = {
    "logs": {"logs": [{"_raw": "minor warning message"}]},
}


# ---------------------------------------------------------------------------
# _enforce_anti_hallucination
# ---------------------------------------------------------------------------

class TestEnforceAntiHallucination:

    def test_sets_hallucination_risk_false_when_coverage_ok(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.50)
        result = {"root_cause": "Connection pool exhausted", "confidence": 80}
        _enforce_anti_hallucination(result, GOOD_EVIDENCE, coverage=0.75)
        assert result["hallucination_risk"] is False
        assert "[UNVERIFIED]" not in result["root_cause"]

    def test_sets_hallucination_risk_true_when_coverage_low(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.70)
        monkeypatch.setattr("supervisor.evidence_citation._AH_MIN_SOURCES", 2)
        result = {"root_cause": "Connection pool exhausted", "confidence": 80}
        _enforce_anti_hallucination(result, GOOD_EVIDENCE, coverage=0.30)
        assert result["hallucination_risk"] is True
        assert "[UNVERIFIED]" in result["root_cause"]

    def test_does_not_flag_insufficient_evidence(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.70)
        monkeypatch.setattr("supervisor.evidence_citation._AH_MIN_SOURCES", 5)
        # Only 1 source — not enough to flag as hallucination
        result = {"root_cause": "Speculative cause", "confidence": 30}
        _enforce_anti_hallucination(result, SPARSE_EVIDENCE, coverage=0.10)
        assert result["hallucination_risk"] is False
        assert "[UNVERIFIED]" not in result["root_cause"]

    def test_does_not_double_append_unverified(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.70)
        monkeypatch.setattr("supervisor.evidence_citation._AH_MIN_SOURCES", 2)
        result = {"root_cause": "Root cause [UNVERIFIED]", "confidence": 30}
        _enforce_anti_hallucination(result, GOOD_EVIDENCE, coverage=0.10)
        assert result["root_cause"].count("[UNVERIFIED]") == 1

    def test_empty_root_cause_no_append(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.70)
        monkeypatch.setattr("supervisor.evidence_citation._AH_MIN_SOURCES", 2)
        result = {"root_cause": "", "confidence": 10}
        _enforce_anti_hallucination(result, GOOD_EVIDENCE, coverage=0.10)
        assert result["root_cause"] == ""

    def test_skips_error_evidence_in_source_count(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.70)
        monkeypatch.setattr("supervisor.evidence_citation._AH_MIN_SOURCES", 2)
        # All evidence has errors — counts as 0 sources
        error_evidence = {
            "logs": {"error": "splunk timeout"},
            "metrics": {"error": "sysdig unavailable"},
            "apm_data": {"error": "dynatrace error"},
        }
        result = {"root_cause": "Some cause", "confidence": 50}
        _enforce_anti_hallucination(result, error_evidence, coverage=0.10)
        # Insufficient sources → no flag
        assert result["hallucination_risk"] is False


# ---------------------------------------------------------------------------
# annotate_citations (end-to-end with anti-hallucination)
# ---------------------------------------------------------------------------

class TestAnnotateCitationsAntiHallucination:

    def test_hallucination_risk_added_to_result(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_FLOOR", 0.95)
        monkeypatch.setattr("supervisor.evidence_citation._AH_MIN_SOURCES", 1)
        monkeypatch.setattr("supervisor.evidence_citation._AH_ENABLED", True)
        result = {
            "root_cause": "Quantum entanglement in the network stack",
            "reasoning": "The quantum layer experienced decoherence.",
            "confidence": 90,
        }
        annotate_citations(result, GOOD_EVIDENCE)
        assert "hallucination_risk" in result

    def test_hallucination_risk_false_when_disabled(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_citation._AH_ENABLED", False)
        result = {
            "root_cause": "Quantum entanglement in the network stack",
            "reasoning": "Something happened.",
            "confidence": 90,
        }
        annotate_citations(result, GOOD_EVIDENCE)
        # When disabled, hallucination_risk should not be set to True
        assert result.get("hallucination_risk") is not True

    def test_citation_coverage_always_computed(self):
        result = {
            "root_cause": "Connection pool exhausted",
            "reasoning": "Pool was exhausted due to connection leak.",
            "confidence": 80,
        }
        annotate_citations(result, GOOD_EVIDENCE)
        assert "citation_coverage" in result
        assert 0.0 <= result["citation_coverage"] <= 1.0

    def test_cited_root_cause_added(self):
        result = {
            "root_cause": "Connection pool exhausted",
            "reasoning": "Pool reached maximum connections.",
            "confidence": 80,
        }
        annotate_citations(result, GOOD_EVIDENCE)
        assert "cited_root_cause" in result
        assert isinstance(result["cited_root_cause"], str)

    def test_does_not_raise_on_empty_evidence(self):
        result = {"root_cause": "Something", "reasoning": "Some reason.", "confidence": 50}
        # Should not raise
        annotate_citations(result, {})
        assert "citation_coverage" in result
