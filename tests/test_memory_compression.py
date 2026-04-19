"""Tests for supervisor.memory_compression."""
from __future__ import annotations

import pytest
from supervisor.memory_compression import (
    compress_investigation,
    compress_turns,
    InvestigationDigest,
    _extractive_fallback,
    _extract_evidence_keys,
    _summarise_timeline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RESULT_COMPLETE = {
    "root_cause": "Connection pool exhausted due to config change",
    "confidence": 82,
    "reasoning": (
        "The connection pool reached maximum capacity after a deployment reduced "
        "MAX_CONNECTIONS from 100 to 10. Error rate spiked immediately. "
        "Rollback of the config change is recommended."
    ),
    "evidence_timeline": [
        {"event": "Deployment v2.1.0 merged", "source": "github", "timestamp": "2024-01-15T14:00:00Z"},
        {"event": "Error rate spike detected", "source": "sysdig", "timestamp": "2024-01-15T14:02:00Z"},
        {"event": "Alert fired", "source": "moogsoft", "timestamp": "2024-01-15T14:03:00Z"},
    ],
    "citations": [
        {"source": "splunk", "claim": "connection pool exhausted"},
        {"source": "github", "claim": "config change reduced connections"},
    ],
    "citation_coverage": 0.85,
    "proposed_fix": {"fix_type": "rollback", "description": "Rollback v2.1.0"},
}

RESULT_MINIMAL = {
    "root_cause": "Unknown",
    "confidence": 20,
    "reasoning": "",
}


# ---------------------------------------------------------------------------
# compress_investigation
# ---------------------------------------------------------------------------

class TestCompressInvestigation:

    def test_returns_investigation_digest(self):
        digest = compress_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="payment-service",
            result=RESULT_COMPLETE,
        )
        assert isinstance(digest, InvestigationDigest)

    def test_fields_populated(self):
        digest = compress_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="payment-service",
            result=RESULT_COMPLETE,
            online_quality_score=0.88,
        )
        assert digest.incident_id == "INC001"
        assert digest.incident_type == "saturation"
        assert digest.service == "payment-service"
        assert digest.root_cause == RESULT_COMPLETE["root_cause"]
        assert digest.confidence == 82
        assert digest.citation_coverage == 0.85
        assert digest.fix_proposed is True
        assert digest.fix_type == "rollback"
        assert digest.quality_score == pytest.approx(0.88, abs=0.001)  # round(0.88, 3)

    def test_to_ltm_text_contains_key_fields(self):
        digest = compress_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="payment-service",
            result=RESULT_COMPLETE,
        )
        text = digest.to_ltm_text()
        assert "INC001" in text
        assert "payment-service" in text
        assert "saturation" in text
        assert "Connection pool" in text

    def test_minimal_result_does_not_raise(self):
        digest = compress_investigation(
            incident_id="INC999",
            incident_type="unknown",
            service="svc",
            result=RESULT_MINIMAL,
        )
        assert digest.incident_id == "INC999"
        assert digest.confidence == 20
        assert digest.fix_proposed is False

    def test_to_dict_is_serialisable(self):
        import json
        digest = compress_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="payment-service",
            result=RESULT_COMPLETE,
        )
        d = digest.to_dict()
        # Should not raise
        json.dumps(d)
        assert d["incident_id"] == "INC001"

    def test_timeline_summary_format(self):
        digest = compress_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="svc",
            result=RESULT_COMPLETE,
        )
        # 3 events → "first → ... (3 events) → last"
        assert "Deployment v2.1.0" in digest.timeline_summary
        assert "Alert fired" in digest.timeline_summary

    def test_source_count_from_citations(self):
        digest = compress_investigation(
            incident_id="INC001",
            incident_type="saturation",
            service="svc",
            result=RESULT_COMPLETE,
        )
        # splunk + github = 2
        assert digest.source_count == 2


# ---------------------------------------------------------------------------
# compress_turns
# ---------------------------------------------------------------------------

class TestCompressTurns:

    def test_empty_turns_returns_empty(self):
        assert compress_turns([]) == ""

    def test_returns_string(self):
        turns = [
            {"role": "user", "content": "What is the root cause?"},
            {"role": "assistant", "content": "Connection pool exhausted due to low MAX_CONNECTIONS."},
        ]
        result = compress_turns(turns)
        assert isinstance(result, str)

    def test_truncates_to_token_limit(self):
        # Very long turns should be truncated
        long_content = "a " * 5000
        turns = [{"role": "user", "content": long_content}]
        result = compress_turns(turns)
        # Should not exceed max_tokens * chars_per_token by much
        assert len(result) < len(long_content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestExtractiveeFallback:

    def test_picks_sentences_with_signal_words(self):
        text = (
            "The weather is nice today. "
            "The connection pool was exhausted due to high load. "
            "We went to the park. "
            "The root cause was a memory leak caused by a misconfiguration."
        )
        result = _extractive_fallback(text, max_chars=500)
        assert "connection pool" in result.lower() or "root cause" in result.lower()

    def test_returns_fallback_when_no_signal(self):
        text = "Nothing relevant here at all."
        result = _extractive_fallback(text, max_chars=500)
        assert isinstance(result, str)
        assert len(result) > 0


class TestExtractEvidenceKeys:

    def test_extracts_from_citations(self):
        result = {
            "citations": [
                {"source": "splunk"},
                {"source": "github"},
                {"source": "splunk"},  # duplicate
            ]
        }
        keys = _extract_evidence_keys(result)
        assert "splunk" in keys
        assert "github" in keys
        # No duplicates
        assert keys.count("splunk") == 1

    def test_extracts_from_timeline(self):
        result = {
            "evidence_timeline": [
                {"source": "sysdig", "event": "metric spike"},
                {"worker": "apm_worker", "event": "error detected"},
            ]
        }
        keys = _extract_evidence_keys(result)
        assert "sysdig" in keys
        assert "apm_worker" in keys


class TestSummariseTimeline:

    def test_empty_returns_empty(self):
        assert _summarise_timeline([]) == ""

    def test_single_event(self):
        events = [{"event": "Alert fired"}]
        result = _summarise_timeline(events)
        assert "Alert fired" in result

    def test_two_events_joined(self):
        events = [{"event": "Deploy"}, {"event": "Rollback"}]
        result = _summarise_timeline(events)
        assert "Deploy" in result
        assert "Rollback" in result

    def test_many_events_abbreviated(self):
        events = [{"event": f"Event {i}"} for i in range(10)]
        result = _summarise_timeline(events)
        assert "Event 0" in result
        assert "Event 9" in result
        assert "10 events" in result
