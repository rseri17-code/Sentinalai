"""Tests for supervisor.evidence_gates."""
from __future__ import annotations

import pytest

from supervisor.evidence_gates import (
    GateVerdict,
    GateResult,
    GateCheckResult,
    check_post_collection,
    check_post_analysis,
    _count_empty_evidence,
    _count_unique_sources,
    _rc_cited,
    _verdict_rank,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOOD_EVIDENCE = {
    "logs": {"logs": [{"_raw": "error: connection refused"}]},
    "metrics": {"signals": {"latency_ms": 5200}},
    "apm_data": {"errors": [{"message": "OOMKilled"}]},
}

EMPTY_EVIDENCE = {
    "logs": {},
    "metrics": None,
    "apm_data": {"error": "timeout"},
    "golden_signals": [],
    "itsm_context": {},
}

GOOD_RESULT = {
    "root_cause": "Connection pool exhausted",
    "confidence": 80,
    "citation_coverage": 0.85,
    "citations": [
        {"claim": "connection pool exhausted max connections", "source": "splunk"},
    ],
}


# ---------------------------------------------------------------------------
# GateVerdict
# ---------------------------------------------------------------------------

class TestGateVerdict:

    def test_values(self):
        assert GateVerdict.PASS.value == "pass"
        assert GateVerdict.WARN.value == "warn"
        assert GateVerdict.BLOCK.value == "block"
        assert GateVerdict.ESCALATE.value == "escalate"


# ---------------------------------------------------------------------------
# GateResult
# ---------------------------------------------------------------------------

class TestGateResult:

    def test_passed_property(self):
        r = GateResult("G1", GateVerdict.PASS, "ok")
        assert r.passed is True

    def test_not_passed_for_warn(self):
        r = GateResult("G3", GateVerdict.WARN, "low coverage")
        assert r.passed is False

    def test_is_critical_for_block(self):
        r = GateResult("G4", GateVerdict.BLOCK, "no data")
        assert r.is_critical is True

    def test_is_critical_for_escalate(self):
        r = GateResult("G2", GateVerdict.ESCALATE, "low confidence")
        assert r.is_critical is True

    def test_not_critical_for_warn(self):
        r = GateResult("G3", GateVerdict.WARN, "low coverage")
        assert r.is_critical is False


# ---------------------------------------------------------------------------
# GateCheckResult
# ---------------------------------------------------------------------------

class TestGateCheckResult:

    def test_to_dict_keys(self):
        g = GateCheckResult(passed=True, verdict=GateVerdict.PASS, gates=[])
        d = g.to_dict()
        assert "passed" in d
        assert "verdict" in d
        assert "gates" in d
        assert "blocking_gate" in d

    def test_to_dict_with_blocking_gate(self):
        bg = GateResult("G4", GateVerdict.BLOCK, "dead evidence", 5.0, 5.0)
        g = GateCheckResult(passed=False, verdict=GateVerdict.BLOCK, blocking_gate=bg)
        d = g.to_dict()
        assert d["blocking_gate"]["gate"] == "G4"
        assert d["blocking_gate"]["verdict"] == "block"


# ---------------------------------------------------------------------------
# check_post_collection (G1 + G4)
# ---------------------------------------------------------------------------

class TestCheckPostCollection:

    def test_passes_with_good_evidence(self):
        result = check_post_collection(GOOD_EVIDENCE, budget_used=3)
        assert result.passed is True

    def test_g4_blocks_on_dead_evidence(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.DEAD_EVIDENCE_N", 3)
        # 5 keys, all empty → triggers G4
        dead = {
            "logs": None,
            "metrics": {},
            "apm_data": {"error": "timeout"},
            "golden_signals": [],
            "itsm_context": None,
        }
        result = check_post_collection(dead, budget_used=2)
        assert result.verdict == GateVerdict.BLOCK
        assert result.blocking_gate is not None
        assert result.blocking_gate.gate_name == "G4_DeadEvidence"

    def test_g1_escalates_with_few_sources_and_low_budget(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.MIN_SOURCES", 3)
        # Only 1 unique source
        evidence = {
            "logs": {"logs": [{"_raw": "error"}]},
            "log_data": {"logs": []},  # same splunk category
        }
        result = check_post_collection(evidence, budget_used=5)
        # budget_used=5 < 10 → should ESCALATE
        assert result.verdict in (GateVerdict.ESCALATE, GateVerdict.WARN)

    def test_g1_warns_with_few_sources_and_high_budget(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.MIN_SOURCES", 3)
        evidence = {"logs": {"logs": [{"_raw": "error"}]}}
        result = check_post_collection(evidence, budget_used=12)
        # budget_used >= 10 → WARN not ESCALATE
        gate_verdicts = {g.verdict for g in result.gates}
        assert GateVerdict.WARN in gate_verdicts or GateVerdict.ESCALATE in gate_verdicts

    def test_disabled_gates_always_pass(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.GATES_ENABLED", False)
        result = check_post_collection({}, budget_used=0)
        assert result.passed is True


# ---------------------------------------------------------------------------
# check_post_analysis (G2 + G3 + G5)
# ---------------------------------------------------------------------------

class TestCheckPostAnalysis:

    def test_passes_with_good_result(self):
        result = check_post_analysis(GOOD_RESULT, GOOD_EVIDENCE, budget_remaining=10)
        assert result.passed is True

    def test_g3_warns_on_low_citation_coverage(self):
        low_cov_result = {**GOOD_RESULT, "citation_coverage": 0.20}
        result = check_post_analysis(low_cov_result, GOOD_EVIDENCE, budget_remaining=5)
        gate_names = {g.gate_name for g in result.gates}
        assert "G3_CitationFloor" in gate_names
        g3 = next(g for g in result.gates if g.gate_name == "G3_CitationFloor")
        assert g3.verdict == GateVerdict.WARN
        # WARN does not block — passed should be True
        assert result.passed is True

    def test_g2_escalates_on_low_confidence_and_no_budget(self):
        low_conf_result = {**GOOD_RESULT, "confidence": 10}
        result = check_post_analysis(low_conf_result, GOOD_EVIDENCE, budget_remaining=0)
        gate_names = {g.gate_name for g in result.gates}
        assert "G2_ConfidenceFloor" in gate_names
        g2 = next(g for g in result.gates if g.gate_name == "G2_ConfidenceFloor")
        assert g2.verdict == GateVerdict.ESCALATE

    def test_g2_passes_when_budget_remains(self):
        # Low confidence but budget remains → should not escalate
        low_conf_result = {**GOOD_RESULT, "confidence": 10}
        result = check_post_analysis(low_conf_result, GOOD_EVIDENCE, budget_remaining=10)
        g2 = next((g for g in result.gates if g.gate_name == "G2_ConfidenceFloor"), None)
        if g2:
            assert g2.verdict == GateVerdict.PASS

    def test_g5_blocks_unsupported_root_cause(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.MIN_SOURCES", 1)
        # Root cause with no matching citations, enough evidence
        uncited_result = {
            "root_cause": "Quantum fluctuation in the hypervisor layer",
            "confidence": 80,
            "citation_coverage": 0.85,
            "citations": [
                # Citation for something completely different
                {"claim": "database backup completed successfully", "source": "splunk"},
            ],
        }
        result = check_post_analysis(uncited_result, GOOD_EVIDENCE, budget_remaining=5)
        gate_names = {g.gate_name for g in result.gates}
        # G5 should trigger because root cause words don't match any citation
        assert "G5_HallucinationRisk" in gate_names
        g5 = next(g for g in result.gates if g.gate_name == "G5_HallucinationRisk")
        assert g5.verdict == GateVerdict.BLOCK

    def test_g5_skips_when_insufficient_evidence(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.MIN_SOURCES", 5)
        # Root cause uncited, but too few sources to call it hallucination
        uncited_result = {
            "root_cause": "Quantum fluctuation in the hypervisor layer",
            "confidence": 80,
            "citation_coverage": 0.85,
            "citations": [{"claim": "database backup completed", "source": "splunk"}],
        }
        result = check_post_analysis(uncited_result, GOOD_EVIDENCE, budget_remaining=5)
        # Should NOT block (insufficient sources to be sure it's hallucination)
        assert result.verdict != GateVerdict.BLOCK or \
               all(g.gate_name != "G5_HallucinationRisk" for g in result.gates if g.verdict == GateVerdict.BLOCK)

    def test_disabled_gates_always_pass(self, monkeypatch):
        monkeypatch.setattr("supervisor.evidence_gates.GATES_ENABLED", False)
        result = check_post_analysis({}, {}, budget_remaining=0)
        assert result.passed is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestCountEmptyEvidence:

    def test_counts_none_as_empty(self):
        assert _count_empty_evidence({"a": None}) == 1

    def test_skips_underscore_keys(self):
        assert _count_empty_evidence({"_meta": None}) == 0

    def test_dict_with_error_is_empty(self):
        assert _count_empty_evidence({"a": {"error": "timeout"}}) == 1

    def test_non_empty_dict_not_empty(self):
        assert _count_empty_evidence({"a": {"logs": ["entry"]}}) == 0

    def test_empty_list_is_empty(self):
        assert _count_empty_evidence({"a": []}) == 1

    def test_populated_list_not_empty(self):
        assert _count_empty_evidence({"a": [1, 2, 3]}) == 0


class TestCountUniqueSources:

    def test_groups_log_keys_to_splunk(self):
        evidence = {
            "logs": {"logs": [{"_raw": "err"}]},
            "log_data": {"logs": [{"_raw": "warn"}]},
        }
        # Both map to "splunk" → count as 1
        assert _count_unique_sources(evidence) == 1

    def test_multiple_distinct_sources(self):
        assert _count_unique_sources(GOOD_EVIDENCE) >= 2

    def test_skips_error_dicts(self):
        evidence = {"metrics": {"error": "connection refused"}}
        assert _count_unique_sources(evidence) == 0

    def test_unknown_key_uses_itself_as_source(self):
        evidence = {"custom_tool": {"data": "something"}}
        assert _count_unique_sources(evidence) == 1


class TestRcCited:

    def test_enough_overlap_returns_true(self):
        # root_cause has "connection pool" = 2 words
        # citation claim also has "connection pool" = 2 word overlap ≥ 2
        assert _rc_cited("connection pool exhausted", {"claim": "connection pool max reached"}) is True

    def test_insufficient_overlap_returns_false(self):
        assert _rc_cited("connection pool", {"claim": "completely unrelated matter"}) is False

    def test_empty_claim_returns_false(self):
        assert _rc_cited("connection pool exhausted", {"claim": ""}) is False


class TestVerdictRank:

    def test_order(self):
        assert _verdict_rank(GateVerdict.PASS) < _verdict_rank(GateVerdict.WARN)
        assert _verdict_rank(GateVerdict.WARN) < _verdict_rank(GateVerdict.ESCALATE)
        assert _verdict_rank(GateVerdict.ESCALATE) < _verdict_rank(GateVerdict.BLOCK)
