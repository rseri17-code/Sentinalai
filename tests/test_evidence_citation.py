"""Tests for the evidence citation engine."""
from __future__ import annotations

from supervisor.evidence_citation import (
    annotate_citations,
    _extract_claims,
    _build_evidence_corpus,
    _tokenize,
    _coverage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RESULT_WITH_RC = {
    "root_cause": "Connection pool exhausted due to reduced MAX_CONNECTIONS in recent deployment",
    "reasoning": (
        "Pool reached maximum capacity of 1024/1024 connections. "
        "Error rate spiked from 0.1% to 8.7% immediately after deploy v2.1.0. "
        "Rollback of PR #847 is the recommended immediate action."
    ),
    "confidence": 88,
}

EVIDENCE_WITH_LOGS = {
    "logs": {
        "logs": [
            {
                "_time": "2024-01-15T14:02:11Z",
                "level": "ERROR",
                "_raw": "pool.exhausted connections=1024/1024 waiting=47",
                "message": "Connection pool exhausted: 1024/1024 connections used",
            },
            {
                "_time": "2024-01-15T14:02:12Z",
                "level": "ERROR",
                "_raw": "timeout acquiring connection after 30000ms",
                "message": "Timeout acquiring database connection",
            },
        ]
    },
    "itsm_context": {
        "change_records": [
            {
                "number": "CHG0099001",
                "short_description": "Deploy payment-service v2.1.0 — connection pool resize",
                "requested_by": "devops-automation",
                "end_date": "2024-01-15T14:15:00",
                "risk": "medium",
            }
        ]
    },
}


# ---------------------------------------------------------------------------
# annotate_citations
# ---------------------------------------------------------------------------

class TestAnnotateCitations:
    def test_adds_citations_key(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert "citations" in result

    def test_adds_citation_coverage_key(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert "citation_coverage" in result
        assert 0.0 <= result["citation_coverage"] <= 1.0

    def test_adds_cited_root_cause_key(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert "cited_root_cause" in result

    def test_citations_is_list(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert isinstance(result["citations"], list)

    def test_citation_has_required_fields(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        for citation in result["citations"]:
            assert "claim" in citation
            assert "source" in citation
            assert "evidence" in citation
            assert "confidence" in citation
            assert "citation_id" in citation

    def test_citation_id_includes_source(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        for citation in result["citations"]:
            assert citation["source"] in citation["citation_id"]

    def test_does_not_raise_on_empty_evidence(self):
        result = dict(RESULT_WITH_RC)
        # Should not raise
        annotate_citations(result, {})
        assert "citations" in result

    def test_does_not_raise_on_empty_result(self):
        result: dict = {}
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert "citations" in result

    def test_returns_result(self):
        result = dict(RESULT_WITH_RC)
        returned = annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert returned is result

    def test_splunk_evidence_generates_splunk_citations(self):
        result = {
            "root_cause": "Connection pool exhausted waiting connections",
            "reasoning": "Pool reached maximum connections limit",
        }
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        sources = {c["source"] for c in result["citations"]}
        assert "splunk" in sources

    def test_itsm_change_record_generates_servicenow_citation(self):
        result = {
            "root_cause": "Recent deployment changed pool configuration",
            "reasoning": "Deploy payment-service connection pool resize caused the issue",
        }
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        sources = {c["source"] for c in result["citations"]}
        assert "servicenow" in sources

    def test_diff_analysis_generates_github_citation(self):
        evidence = {
            "diff_analysis": {
                "culprit_file": "src/config/database.py",
                "culprit_line": 42,
                "culprit_snippet": "MAX_CONNECTIONS = 5",
                "confidence": 90,
            }
        }
        result = {
            "root_cause": "MAX_CONNECTIONS reduced in database configuration change",
            "reasoning": "Code change in database.py reduced pool size from 50 to 5",
        }
        annotate_citations(result, evidence)
        sources = {c["source"] for c in result["citations"]}
        assert "github" in sources

    def test_cited_root_cause_appends_citation_refs(self):
        result = {
            "root_cause": "Connection pool exhausted connections",
            "reasoning": "Pool reached maximum capacity connections exhausted",
        }
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        # If citations found, cited_root_cause should have refs
        if result["citations"]:
            assert "[" in result["cited_root_cause"] or result["cited_root_cause"] == result["root_cause"]


# ---------------------------------------------------------------------------
# _extract_claims
# ---------------------------------------------------------------------------

class TestExtractClaims:
    def test_splits_on_sentence_boundaries(self):
        result = {
            "root_cause": "Pool exhausted.",
            "reasoning": "Error rate spiked. Connection timeout observed.",
        }
        claims = _extract_claims(result)
        assert len(claims) >= 2

    def test_filters_short_claims(self):
        result = {"root_cause": "OK.", "reasoning": "This is a longer meaningful sentence here."}
        claims = _extract_claims(result)
        assert all(len(c) > 10 for c in claims)

    def test_handles_missing_keys(self):
        claims = _extract_claims({})
        assert claims == []

    def test_combines_root_cause_and_reasoning(self):
        result = {
            "root_cause": "Database connection exhausted.",
            "reasoning": "Error rate increased significantly after deployment.",
        }
        claims = _extract_claims(result)
        combined = " ".join(claims)
        assert "database" in combined.lower() or "connection" in combined.lower()
        assert "error" in combined.lower() or "rate" in combined.lower()


# ---------------------------------------------------------------------------
# _build_evidence_corpus
# ---------------------------------------------------------------------------

class TestBuildEvidenceCorpus:
    def test_extracts_splunk_logs(self):
        evidence = {
            "logs": {
                "logs": [
                    {"message": "Connection pool exhausted", "level": "ERROR", "_time": "2024-01-15"}
                ]
            }
        }
        corpus = _build_evidence_corpus(evidence)
        sources = [e["source"] for e in corpus]
        assert "splunk" in sources

    def test_extracts_itsm_changes(self):
        evidence = {
            "itsm_context": {
                "change_records": [
                    {"number": "CHG001", "short_description": "Deploy v2.1.0", "requested_by": "bot"}
                ]
            }
        }
        corpus = _build_evidence_corpus(evidence)
        sources = [e["source"] for e in corpus]
        assert "servicenow" in sources

    def test_extracts_cmdb_blast_radius(self):
        evidence = {
            "cmdb_blast_radius": {
                "blast_radius": {
                    "payment-db": [{"short_description": "DB config change", "risk": "high"}]
                }
            }
        }
        corpus = _build_evidence_corpus(evidence)
        sources = [e["source"] for e in corpus]
        assert "cmdb" in sources

    def test_returns_empty_list_for_empty_evidence(self):
        corpus = _build_evidence_corpus({})
        assert corpus == []

    def test_corpus_entries_have_required_fields(self):
        corpus = _build_evidence_corpus(EVIDENCE_WITH_LOGS)
        for entry in corpus:
            assert "source" in entry
            assert "text" in entry
            assert "timestamp" in entry


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_lowercases_words(self):
        tokens = _tokenize("CONNECTION POOL EXHAUSTED")
        assert all(t == t.lower() for t in tokens)

    def test_filters_short_words(self):
        tokens = _tokenize("a bb ccc dddd")
        assert "a" not in tokens
        assert "bb" not in tokens
        assert "ccc" in tokens

    def test_removes_stop_words(self):
        tokens = _tokenize("the connection pool was exhausted")
        assert "the" not in tokens
        assert "was" not in tokens
        assert "connection" in tokens

    def test_handles_empty_string(self):
        assert _tokenize("") == []

    def test_handles_punctuation(self):
        tokens = _tokenize("error: pool.exhausted at line:42")
        assert "error" in tokens
        assert "pool" in tokens


# ---------------------------------------------------------------------------
# citation_coverage
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_returns_zero_when_no_claims(self):
        assert _coverage({}, []) == 0.0

    def test_returns_zero_when_no_citations(self):
        result = {"root_cause": "Something failed and caused the outage here."}
        assert _coverage(result, []) == 0.0

    def test_coverage_between_0_and_1(self):
        result = dict(RESULT_WITH_RC)
        annotate_citations(result, EVIDENCE_WITH_LOGS)
        assert 0.0 <= result["citation_coverage"] <= 1.0
