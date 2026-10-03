"""Evidence-bound timeout causes. Answers are fixed before the run.

Cases:
  (a) pool exhaustion
  (b) supported slow queries
  (c) elevated latency, cause unknown
  (d) missing evidence (a summary that names the pool is not evidence)
  (e) conflicting pool and slow-query records
"""
from __future__ import annotations

import re

from supervisor.agent import Hypothesis, SentinalAISupervisor
from supervisor.helpers.cause_binding import bind_hypothesis
from supervisor.replay import REPLAY_HASH_FIELDS, replay_result_hash
from supervisor.evidence_citation import annotate_citations
from supervisor.helpers.confidence import compute_confidence
from supervisor.helpers.placeholders import is_placeholder
from supervisor.helpers.timeout_evidence import (
    ALIGNMENT_WINDOW_MINUTES,
    attach_cited_outputs,
    resolve_evidence_ref,
    ref_in_window,
)

INCIDENT = {
    "incident_id": "INC-T",
    "affected_service": "payment-service",
    "summary": "payment-service request timeout — upstream payment-db not responding",
    "start_time": "2024-06-21T03:44:21Z",
}

# A source summary that states a cause. It must never be cited.
_DECOY_SUMMARY = (
    "connection pool exhausted because of slow queries, query plan, "
    "index, lock contention, and replication lag on payment-db"
)


def _logs_blob(records, sequence_order=2):
    return {
        "_receipt_sequence_order": sequence_order,
        "_receipt_tool": "log_worker",
        "summary": _DECOY_SUMMARY,
        "note": "annotation: slow queries caused the pool to exhaust",
        "logs": {"results": records, "count": len(records)},
    }


def _signals_blob(p95=2500, baseline=80, sequence_order=3):
    return {
        "_receipt_sequence_order": sequence_order,
        "_receipt_tool": "apm_worker",
        "summary": _DECOY_SUMMARY,
        "signals": {
            "anomaly_start": "2024-06-21T03:44:10Z",
            "anomaly_detected": True,
            "golden_signals": {
                "latency": {"p95": p95, "baseline_p95": baseline},
            },
        },
    }


def _timeout_line():
    return {
        "_time": "2024-06-21T03:44:18Z",
        "service": "payment-service",
        "message": "ERROR timeout waiting for connection: payment-db",
    }


def _pool_line():
    return {
        "_time": "2024-06-21T03:44:19Z",
        "service": "payment-service",
        "message": "ERROR pool.exhausted service=payment-service waiting=47",
    }


def _slow_line():
    return {
        "_time": "2024-06-21T03:44:20Z",
        "service": "payment-service",
        "message": "ERROR slow query on payment-db: SELECT id FROM payments took 28450ms",
    }


def _run(evidence):
    sup = SentinalAISupervisor()
    sup._tls.current_incident = dict(INCIDENT)
    sup._tls.last_evidence = evidence
    result = sup._analyze_evidence("INC-T", dict(INCIDENT), "timeout", evidence)
    annotate_citations(result, evidence)
    return result


def _assert_refs_resolve(result, evidence):
    incident = dict(INCIDENT)
    groups = []
    cause = result["cause"]
    groups.extend(cause["evidence_refs"])
    for group in cause["contradictions"]:
        groups.extend(group["evidence_refs"])
    groups.extend(result["symptom"]["evidence_refs"])
    for ref in groups:
        record = resolve_evidence_ref(ref, evidence, result.get("receipts"))
        assert record is not None, ref
        assert ref_in_window(ref, incident), ref
        # The ref keeps the record's own service. payment-db is named by the
        # timeout line; it is not copied onto the pool or latency record.
        own = record.get("service") if isinstance(record, dict) else ""
        if isinstance(own, str) and own:
            assert ref["service"] == own, ref
        else:
            assert ref["service"] in ("", None) or result["cause"]["category"] == "unknown"
        # The locator must not be a summary/note/annotation field.
        assert "summary" not in ref["locator"]["path"]
        assert "note" not in ref["locator"]["path"]
        assert "annotation" not in ref["locator"]["path"]
        blob = evidence[ref["locator"]["evidence_key"]]
        assert blob.get("_receipt_sequence_order") == ref["sequence_order"]


def _no_confidence_without_citations(result):
    if result["cause"]["confidence"] / 100 >= 0.60:
        assert result["citations"], result["cause"]


def _provenance_balances(result):
    prov = result["_confidence_provenance"]
    assert prov["alignment_window_minutes"] == ALIGNMENT_WINDOW_MINUTES
    total = prov["base"] + sum(c["delta"] for c in prov["contributions"])
    assert int(round(total)) == prov["final_confidence"]
    assert prov["final_confidence"] == result["confidence"]
    assert prov["final_confidence"] == result["cause"]["confidence"]
    symptom_total = prov["symptom_base"] + sum(
        c["delta"] for c in prov["symptom_contributions"]
    )
    assert int(round(symptom_total)) == prov["symptom_confidence"]
    assert prov["symptom_confidence"] == result["symptom"]["confidence"]


class TestEvidenceBoundCause:
    def test_pool_exhaustion(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line()]),
            "check_golden_signals": _signals_blob(),
        }
        result = _run(evidence)
        cause = result["cause"]
        assert cause["statement"] == "connection pool for payment-db exhausted"
        assert result["root_cause"] == cause["statement"]
        assert cause["category"] == "connection_pool_exhaustion"
        assert cause["confidence"] == 62
        assert result["confidence"] == 62
        assert result["symptom"]["statement"] == "timeout observed"
        assert result["symptom"]["confidence"] == 85
        assert cause["confidence"] < result["symptom"]["confidence"]
        assert any("why payment-db refuses connections" == u for u in cause["unknowns"])
        assert cause["contradictions"] == []
        assert {r["signal"] for r in cause["evidence_refs"]} == {"connection_pool_exhausted"}
        for ref in cause["evidence_refs"]:
            record = resolve_evidence_ref(ref, evidence)
            assert "pool.exhausted" in record["message"]
            assert "p95" not in record
        text = result["root_cause"].lower()
        for forbidden in ("slow quer", "query plan", "index", "lock contention", "replication lag"):
            assert forbidden not in text
        _assert_refs_resolve(result, evidence)
        _no_confidence_without_citations(result)
        _provenance_balances(result)
        # The decoy summary is not a cited record.
        cited_messages = []
        for ref in cause["evidence_refs"] + result["symptom"]["evidence_refs"]:
            rec = resolve_evidence_ref(ref, evidence)
            cited_messages.append(rec.get("message", ""))
        assert all("query plan" not in m for m in cited_messages)

    def test_driver_wording_is_not_one_literal(self):
        """Pool and slow-query matches follow the record's meaning.

        Hikari says "connection is not available"; another driver says
        "query exceeded". Neither line is the literal "pool.exhausted"
        or "slow query".
        """
        evidence = {
            "search_timeout_logs": _logs_blob([
                _timeout_line(),
                {
                    "_time": "2024-06-21T03:44:19Z",
                    "service": "payment-service",
                    "message": "HikariPool-1 - Connection is not available, request timed out after 30000ms",
                },
            ]),
        }
        pool = _run(evidence)
        assert pool["cause"]["statement"] == "connection pool for payment-db exhausted"
        assert pool["cause"]["confidence"] == 62

        slow = _run({
            "search_timeout_logs": _logs_blob([
                _timeout_line(),
                {
                    "_time": "2024-06-21T03:44:20Z",
                    "service": "payment-db",
                    "message": "postgres query exceeded 25s: UPDATE payment_transactions",
                },
            ]),
        })
        assert slow["cause"]["statement"] == "slow queries on payment-db"
        assert slow["cause"]["confidence"] == 62

    def test_supported_slow_queries(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _slow_line()]),
            "check_golden_signals": _signals_blob(),
        }
        result = _run(evidence)
        cause = result["cause"]
        assert cause["statement"] == "slow queries on payment-db"
        assert cause["category"] == "slow_queries"
        assert cause["confidence"] == 62
        assert result["symptom"]["confidence"] == 85
        assert {r["signal"] for r in cause["evidence_refs"]} == {"slow_query"}
        for ref in cause["evidence_refs"]:
            record = resolve_evidence_ref(ref, evidence)
            assert "slow query" in record["message"].lower()
            assert "p95" not in record
        _assert_refs_resolve(result, evidence)
        _no_confidence_without_citations(result)
        _provenance_balances(result)

    def test_elevated_latency_cause_unknown(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line()]),
            "check_golden_signals": _signals_blob(),
        }
        result = _run(evidence)
        cause = result["cause"]
        # The p95 summary is a derived conclusion. It does not support a cause.
        # No raw metric point was retrieved, so nothing binds.
        assert cause["statement"] == "timeout observed; cause UNKNOWN"
        assert cause["category"] == "unknown"
        assert cause["confidence"] == 12
        assert cause["confidence"] < 60
        assert cause["evidence_refs"] == []
        assert result["symptom"]["confidence"] == 85
        _assert_refs_resolve(result, evidence)
        _no_confidence_without_citations(result)
        _provenance_balances(result)

    def test_missing_evidence(self):
        # Pool wording lives only in summary/note, plus one raw line outside
        # the window and one in-window line that is not a timeout.
        evidence = {
            "search_timeout_logs": _logs_blob([
                {
                    "_time": "2024-01-01T00:00:00Z",
                    "service": "payment-service",
                    "message": "ERROR pool.exhausted service=payment-service waiting=47 payment-db",
                },
                {
                    "_time": "2024-06-21T03:44:18Z",
                    "service": "payment-service",
                    "message": "INFO healthcheck ok",
                },
            ]),
            "_sources_unavailable": ["metrics"],
        }
        result = _run(evidence)
        cause = result["cause"]
        assert cause["statement"] == "timeout observed; cause UNKNOWN"
        assert result["root_cause"] == cause["statement"]
        assert cause["category"] == "unknown"
        assert cause["confidence"] == 12
        assert cause["confidence"] < 60
        assert cause["evidence_refs"] == []
        assert result["symptom"]["confidence"] < 60
        assert any(u.startswith("unavailable:") for u in cause["unknowns"])
        assert "pool" not in cause["statement"]
        _no_confidence_without_citations(result)
        _provenance_balances(result)

    def test_conflicting_evidence(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line(), _slow_line()]),
            "check_golden_signals": _signals_blob(),
        }
        result = _run(evidence)
        cause = result["cause"]
        assert cause["statement"] == "timeout observed; cause UNKNOWN"
        assert cause["category"] == "unknown"
        assert cause["confidence"] < 60
        assert cause["confidence"] == 24
        assert len(cause["contradictions"]) == 2
        signals = {
            ref["signal"]
            for group in cause["contradictions"]
            for ref in group["evidence_refs"]
        }
        assert signals == {"connection_pool_exhausted", "slow_query"}
        assert result["symptom"]["confidence"] >= 60
        _assert_refs_resolve(result, evidence)
        _no_confidence_without_citations(result)
        _provenance_balances(result)

    def test_confidence_ordering(self):
        pool = _run({
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line()]),
            "check_golden_signals": _signals_blob(),
        })
        slow = _run({
            "search_timeout_logs": _logs_blob([_timeout_line(), _slow_line()]),
            "check_golden_signals": _signals_blob(),
        })
        elevated = _run({
            "search_timeout_logs": _logs_blob([_timeout_line()]),
            "check_golden_signals": _signals_blob(),
        })
        conflict = _run({
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line(), _slow_line()]),
        })
        missing = _run({
            "search_timeout_logs": _logs_blob([
                {"_time": "2024-06-21T03:44:18Z", "service": "payment-service",
                 "message": "INFO healthcheck ok"},
            ]),
        })
        # A derived latency summary adds nothing, so it ties the empty case.
        # A contradicted pair stays under 60 and above that empty case.
        assert pool["confidence"] == slow["confidence"] == 62
        assert pool["confidence"] > conflict["confidence"] > elevated["confidence"]
        assert elevated["confidence"] == missing["confidence"]
        assert pool["confidence"] >= 60
        assert elevated["confidence"] < 60
        assert conflict["confidence"] < 60
        assert missing["confidence"] < 60

    def test_no_confidence_without_citations_helper(self):
        """Fail if cause confidence/100 >= 0.60 and citations are empty."""
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line()]),
        }
        result = _run(evidence)
        assert result["cause"]["confidence"] / 100 >= 0.60
        assert result["citations"]
        # Every citation's signal supports the claim it is attached to.
        for citation in result["citations"]:
            if citation["claim"] == result["cause"]["statement"]:
                assert citation["signal"] == "connection_pool_exhausted"


class TestPlaceholderValues:
    """v1.2: none / unknown / "" / null / empty containers are no data."""

    def _metrics_blob(self, metrics_body, sequence_order=6):
        return {
            "_receipt_sequence_order": sequence_order,
            "_receipt_tool": "metrics_worker",
            "metrics": metrics_body,
        }

    def test_pattern_none_adds_nothing_to_symptom_or_cause(self):
        base = {
            "search_timeout_logs": _logs_blob([_timeout_line()]),
            "check_golden_signals": _signals_blob(),
        }
        with_placeholder = {
            **base,
            "check_latency_metrics": self._metrics_blob({
                "metrics": [],
                "pattern": "none",
                "baseline": None,
            }),
        }
        plain = _run(base)
        marked = _run(with_placeholder)
        assert marked["cause"]["confidence"] == plain["cause"]["confidence"]
        assert marked["symptom"]["confidence"] == plain["symptom"]["confidence"]
        cited = [
            ref["locator"]["evidence_key"]
            for ref in marked["cause"]["evidence_refs"] + marked["symptom"]["evidence_refs"]
        ]
        assert "check_latency_metrics" not in cited

    def test_real_metric_counts_and_placeholder_does_not(self):
        placeholder_only = {
            "search_timeout_logs": _logs_blob([_timeout_line()]),
            "check_latency_metrics": self._metrics_blob({
                "metrics": [{
                    "name": "response_time_ms",
                    "timestamp": "2024-06-21T03:44:10Z",
                    "value": "none",
                }],
                "baseline": "unknown",
                "pattern": " NONE ",
            }),
        }
        mixed_body = {
            "metrics": [
                {
                    "name": "response_time_ms",
                    "timestamp": "2024-06-21T03:44:09Z",
                    "value": "none",
                },
                {
                    "name": "response_time_ms",
                    "timestamp": "2024-06-21T03:44:10Z",
                    "value": 2500,
                },
            ],
            "baseline": 80,
            "pattern": "none",
        }
        mixed = {
            "search_timeout_logs": _logs_blob([_timeout_line()]),
            "check_latency_metrics": self._metrics_blob(mixed_body),
        }
        real_only = {
            "search_timeout_logs": _logs_blob([_timeout_line()]),
            "check_latency_metrics": self._metrics_blob({
                "metrics": [{
                    "name": "response_time_ms",
                    "timestamp": "2024-06-21T03:44:10Z",
                    "value": 2500,
                }],
                "baseline": 80,
            }),
        }
        absent = _run(placeholder_only)
        both = _run(mixed)
        real = _run(real_only)
        assert absent["cause"]["confidence"] == 12
        assert absent["symptom"]["confidence"] == 70
        assert both["cause"]["statement"] == "payment-db latency elevated; cause UNKNOWN"
        assert both["cause"]["confidence"] == real["cause"]["confidence"] == 34
        assert both["symptom"]["confidence"] == real["symptom"]["confidence"] == 85
        record = resolve_evidence_ref(both["cause"]["evidence_refs"][0], mixed)
        assert record["value"] == 2500
        assert not is_placeholder(record["value"])
        assert "pattern" not in both["cause"]["evidence_refs"][0]["locator"]["path"]

    def test_inc12345_pattern_none_bonus_removed(self):
        """The legacy +1 for pattern 'none' is gone on INC12345's metric points."""
        logs = [
            {"message": "upstream request timeout: payment-service:8080"},
            {"message": "upstream request timeout: payment-service:8080"},
        ]
        signals = {
            "golden_signals": {"latency": {"p95": 31000, "baseline_p95": 200}},
            "anomaly_detected": True,
        }
        points = {
            "metrics": [
                {"name": "response_time_ms", "timestamp": "2024-02-12T10:30:10Z", "value": 31000},
                {"name": "response_time_ms", "timestamp": "2024-02-12T10:30:11Z", "value": 30500},
                {"name": "response_time_ms", "timestamp": "2024-02-12T10:30:12Z", "value": 31200},
            ],
            "baseline": 200,
        }
        changes = [{"number": "CHG0045678"}]
        without = compute_confidence(80, logs, signals, points, [], changes)
        with_none = compute_confidence(80, logs, signals, {**points, "pattern": "none"}, [], changes)
        assert with_none == without == 92
        # Pre-v1.2, pattern "none" was a non-empty string and scored 93.


class TestRefResolution:
    def test_locator_resolves_inside_receipt_result(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line()], sequence_order=4),
        }
        result = _run(evidence)
        ref = result["cause"]["evidence_refs"][0]
        assert ref["sequence_order"] == 4
        assert ref["tool"] == "log_worker"
        record = resolve_evidence_ref(ref, evidence)
        assert record["message"].startswith("ERROR pool.exhausted")
        # A wrong sequence does not resolve.
        bad = dict(ref)
        bad["sequence_order"] = 99
        assert resolve_evidence_ref(bad, evidence) is None

    def test_cited_ref_resolves_from_receipt_output(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line()], sequence_order=4),
        }
        result = _run(evidence)

        class _Receipt:
            def __init__(self):
                self.sequence_order = 4
                self.output = None

        class _Collector:
            def __init__(self):
                self.receipts = [_Receipt()]

        collector = _Collector()
        attach_cited_outputs(result, evidence, collector)
        receipt = collector.receipts[0]
        assert receipt.output is not None
        ref = result["cause"]["evidence_refs"][0]
        # Snapshot is not the record store. The receipt output is.
        record = resolve_evidence_ref(
            ref, {}, receipts=[{"sequence_order": 4, "output": receipt.output}],
        )
        assert record is not None
        assert record["message"].startswith("ERROR pool.exhausted")

    def test_snapshot_key_matches_locator(self):
        evidence = {
            "search_timeout_logs": _logs_blob([_timeout_line(), _pool_line()]),
        }
        result = _run(evidence)
        # _analyze_evidence does not build the bool snapshot; the analyze
        # phase does. The locator's evidence key is present in the corpus.
        for ref in result["cause"]["evidence_refs"]:
            key = ref["locator"]["evidence_key"]
            assert key in evidence
            assert evidence[key]


class TestEveryWinnerIsRebound:
    """AC2/AC3 apply to every incident type. Historical matches are not evidence."""

    def test_historical_npe_is_not_this_incident(self):
        hyp = Hypothesis(
            name="historical_pattern",
            root_cause="deployment v3.1.0 introduced NullPointerException in payment-service",
            base_score=55,
            evidence_refs=["_past_experiences"],
            reasoning="a previous incident said so",
        )
        # A log from this incident that does NOT contain the exception.
        evidence = {
            "search_error_logs": {
                "_receipt_sequence_order": 3,
                "_receipt_tool": "log_worker",
                "logs": {"results": [{
                    "_time": "2024-06-21T03:44:18Z",
                    "message": "ERROR upstream reset",
                    "service": "payment-service",
                }]},
            }
        }
        assessment = bind_hypothesis(
            hyp,
            incident_type="error_spike",
            service="payment-service",
            incident={"start_time": "2024-06-21T03:44:21Z", "affected_service": "payment-service"},
            evidence=evidence,
        )
        assert assessment["category"] == "unknown"
        assert assessment["cause_confidence"] < 60
        assert "NullPointerException" not in assessment["statement"]
        assert "UNKNOWN" in assessment["statement"]

    def test_npe_in_this_incidents_logs_is_kept(self):
        evidence = {
            "search_error_logs": {
                "_receipt_sequence_order": 3,
                "_receipt_tool": "log_worker",
                "logs": {"results": [{
                    "_time": "2024-06-21T03:44:18Z",
                    "message": "NullPointerException in handler",
                    "service": "payment-service",
                }]},
            }
        }
        sup = SentinalAISupervisor()
        incident = {
            "incident_id": "INC-NPE",
            "affected_service": "payment-service",
            "summary": "Payment service error spike",
            "start_time": "2024-06-21T03:44:21Z",
        }
        result = sup._analyze_evidence("INC-NPE", incident, "error_spike", evidence)
        assert "NullPointerException" in result["root_cause"]
        assert result["confidence"] >= 60
        assert result["cause"]["confidence"] >= 60
        assert result["cause"]["evidence_refs"]

    def test_pool_clause_kept_cascade_clause_dropped(self):
        hyp = Hypothesis(
            name="pool_exhaustion_cascade",
            root_cause=(
                "database connection pool exhaustion in payment-service "
                "caused by slow queries after index drop, cascading to checkout"
            ),
            base_score=73,
            evidence_refs=["logs:pool_exhaustion", "logs:cascade_chain"],
            reasoning="template",
        )
        evidence = {
            "search_error_logs": {
                "_receipt_sequence_order": 2,
                "_receipt_tool": "log_worker",
                "logs": {"results": [{
                    "_time": "2024-06-21T03:44:19Z",
                    "service": "payment-service",
                    "message": "Connection pool exhausted: 50/50 connections in use",
                }]},
            }
        }
        assessment = bind_hypothesis(
            hyp,
            incident_type="cascading",
            service="payment-service",
            incident={"start_time": "2024-06-21T03:44:21Z"},
            evidence=evidence,
        )
        assert "connection pool exhausted" in assessment["statement"]
        assert "cascad" not in assessment["statement"].lower()
        assert "slow quer" not in assessment["statement"].lower()
        assert assessment["cause_confidence"] >= 60
        assert any("cascade" in u or "slow" in u or "index" in u or "change" in u
                   for u in assessment["unknowns"])

    def test_replay_fieldset_includes_symptom_and_cause(self):
        assert "symptom" in REPLAY_HASH_FIELDS
        assert "cause" in REPLAY_HASH_FIELDS
        base = {
            "incident_id": "INC1",
            "incident_type": "error_spike",
            "root_cause": "error_spike observed; cause UNKNOWN",
            "confidence": 12,
            "winner_hypothesis": "historical_pattern",
            "symptom": {"statement": "error_spike observed", "confidence": 12, "evidence_refs": []},
            "cause": {"statement": "error_spike observed; cause UNKNOWN", "category": "unknown", "confidence": 12},
        }
        changed = dict(base)
        changed["cause"] = dict(base["cause"])
        changed["cause"]["statement"] = "NullPointerException in payment-service"
        changed["root_cause"] = changed["cause"]["statement"]
        assert replay_result_hash(base) != replay_result_hash(changed)


class TestLlmRefineGate:
    """AC10: an LLM-replaced hypothesis is re-scored from its cited refs."""

    def test_unref_cited_rewrite_cannot_exceed_59(self, monkeypatch):
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setattr("supervisor.llm.LLM_ENABLED", True)
        monkeypatch.setattr("supervisor.agent._llm_enabled", lambda: True)

        def _fake_refine(incident_type, service, summary, evidence_summary, hypotheses, pil_context=""):
            rewritten = []
            for h in hypotheses:
                rewritten.append({
                    "name": h["name"],
                    "root_cause": "disk full because of a leak on an uncited host",
                    "score": 97,
                    "reasoning": "the model is sure",
                })
            return {
                "refined_hypotheses": rewritten,
                "input_tokens": 1,
                "output_tokens": 1,
                "latency_ms": 1,
                "model_id": "stub",
            }

        monkeypatch.setattr("supervisor.agent._llm_refine", _fake_refine)
        sup = SentinalAISupervisor()
        incident = {
            "incident_id": "INC-LLM",
            "affected_service": "payment-service",
            "summary": "Payment service error spike",
            "start_time": "2024-06-21T03:44:21Z",
        }
        evidence = {
            "search_error_logs": {
                "_receipt_sequence_order": 2,
                "_receipt_tool": "log_worker",
                "logs": {"results": [{
                    "_time": "2024-06-21T03:44:18Z",
                    "message": "NullPointerException in handler",
                    "service": "payment-service",
                }]},
            }
        }
        result = sup._analyze_evidence("INC-LLM", incident, "error_spike", evidence)
        assert "disk full" not in result["root_cause"].lower()
        # The rewrite is unsupported. The log's NullPointerException is a
        # direct cause, so the published statement names that and not UNKNOWN.
        assert "NullPointerException" in result["cause"]["statement"]
        assert result["confidence"] >= 60
        assert result["confidence"] != 97
        assert result["cause"]["confidence"] != 97

    def test_score_bump_keeps_cited_cause(self, monkeypatch):
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setattr("supervisor.llm.LLM_ENABLED", True)
        monkeypatch.setattr("supervisor.agent._llm_enabled", lambda: True)
        called = {}

        def _fake_refine(incident_type, service, summary, evidence_summary, hypotheses, pil_context=""):
            called["yes"] = True
            rewritten = []
            for h in hypotheses:
                rewritten.append({
                    "name": h["name"],
                    "root_cause": h["root_cause"],
                    "score": 97,
                    "reasoning": h.get("reasoning", ""),
                })
            return {
                "refined_hypotheses": rewritten,
                "input_tokens": 1,
                "output_tokens": 1,
                "latency_ms": 1,
                "model_id": "stub",
            }

        monkeypatch.setattr("supervisor.agent._llm_refine", _fake_refine)
        sup = SentinalAISupervisor()
        incident = {
            "incident_id": "INC-LLM2",
            "affected_service": "payment-service",
            "summary": "Payment service error spike",
            "start_time": "2024-06-21T03:44:21Z",
        }
        evidence = {
            "search_error_logs": {
                "_receipt_sequence_order": 2,
                "_receipt_tool": "log_worker",
                "logs": {"results": [{
                    "_time": "2024-06-21T03:44:18Z",
                    "message": "NullPointerException in handler",
                    "service": "payment-service",
                }]},
            }
        }
        result = sup._analyze_evidence("INC-LLM2", incident, "error_spike", evidence)
        assert called.get("yes") is True
        assert "NullPointerException" in result["root_cause"]
        assert result["confidence"] >= 60
        assert result["confidence"] != 97
        assert result["cause"]["confidence"] <= 100


class TestGatewayTruncation:
    """Truncation comes from the payload. A full list is not a complete search."""

    def test_truncated_true_uses_gateway_timestamps(self):
        from supervisor.helpers.cause_binding import unchecked_coverage

        evidence = {
            "search_logs": {
                "logs": {
                    "results": [{
                        "_time": "1999-01-01T00:00:00Z",
                        "message": "older than the gateway span",
                    }],
                    "count": 1,
                },
                "limit": 1,
                "truncated": True,
                "oldest_ts": "2024-11-04T07:00:00Z",
                "newest_ts": "2024-11-04T07:59:00Z",
            }
        }
        coverage = unchecked_coverage(None, evidence)
        row = coverage["truncations"][0]
        assert row["evidence_key"] == "search_logs"
        assert row["truncated"] is True
        assert row["truncation_unknown"] is False
        assert row["oldest_ts"] == "2024-11-04T07:00:00Z"
        assert row["newest_ts"] == "2024-11-04T07:59:00Z"
        assert coverage["truncation_unknown"] is False

    def test_truncated_false_is_an_explicit_report(self):
        from supervisor.helpers.cause_binding import unchecked_coverage

        evidence = {
            "query_metrics": {
                "metrics": {
                    "metrics": [{"timestamp": "2024-11-04T07:15:00Z"}],
                    "truncated": False,
                    "oldest_ts": "2024-11-04T07:10:00Z",
                    "newest_ts": "2024-11-04T07:40:00Z",
                    "range": "30m",
                    "step": "30s",
                }
            }
        }
        coverage = unchecked_coverage(None, evidence)
        row = coverage["truncations"][0]
        assert row["truncated"] is False
        assert row["truncation_unknown"] is False
        assert row["oldest_ts"] == "2024-11-04T07:10:00Z"
        assert row["newest_ts"] == "2024-11-04T07:40:00Z"
        assert coverage["truncation_unknown"] is False

    def test_missing_truncated_is_unknown_even_when_count_equals_limit(self):
        from supervisor.helpers.cause_binding import unchecked_coverage

        evidence = {
            "search_logs": {
                "logs": {
                    "results": [{"message": "row", "_time": "2024-11-04T07:30:00Z"}] * 50,
                    "count": 50,
                },
                "limit": 50,
            }
        }
        coverage = unchecked_coverage(None, evidence)
        row = coverage["truncations"][0]
        assert row == {"evidence_key": "search_logs", "truncation_unknown": True}
        assert coverage["truncation_unknown"] is True


# Directions fixed before the v1.8 run. One in-window raw record is
# 20 + 22 + 20. Each further agreeing raw record adds 8. A derived record
# adds 0. An out-of-window record adds 0. A contradicted pair stays at 24.
_ONE_RAW = 20 + 22 + 20
_TWO_RAW = _ONE_RAW + 8
_DOWNSTREAM_UNKNOWN = "which downstream this resource connects to"


def _v18_incident(service, start="2024-08-01T12:00:00Z"):
    return {
        "incident_id": "INC-V18",
        "affected_service": service,
        "summary": f"{service} timeout",
        "start_time": start,
    }


def _v18_timeout(service, records, signals=None):
    incident = _v18_incident(service)
    evidence = {
        "search_timeout_logs": {
            "_receipt_sequence_order": 2,
            "_receipt_tool": "log_worker",
            "logs": {"results": records, "count": len(records)},
        }
    }
    if signals is not None:
        evidence["check_signals"] = signals
    sup = SentinalAISupervisor()
    sup._tls.current_incident = dict(incident)
    sup._tls.last_evidence = evidence
    return sup._analyze_evidence("INC-V18", dict(incident), "timeout", evidence), evidence


def _pool_line_at(service, when, message="connection pool exhausted"):
    return {
        "_time": when,
        "service": service,
        "level": "ERROR",
        "message": message,
    }


def _classes(refs):
    return {ref.get("evidence_class") for ref in refs}


class TestDerivedAndComputedConfidence:
    """v1.8. Expected directions were written before this class ran."""

    def test_second_raw_record_raises_confidence(self):
        # (a) A second direct, aligned, raw record that agrees scores higher.
        one, _ = _v18_timeout("edge-api", [
            _pool_line_at("edge-api", "2024-08-01T12:00:10Z"),
        ])
        two, _ = _v18_timeout("edge-api", [
            _pool_line_at("edge-api", "2024-08-01T12:00:10Z"),
            _pool_line_at("edge-api", "2024-08-01T12:00:40Z"),
        ])
        assert one["cause"]["confidence"] == _ONE_RAW
        assert two["cause"]["confidence"] == _TWO_RAW
        assert two["cause"]["confidence"] > one["cause"]["confidence"]
        assert len(two["cause"]["evidence_refs"]) == 2
        assert _classes(two["cause"]["evidence_refs"]) == {"raw"}

    def test_contradicting_raw_record_stays_below_60(self):
        # (b) An unresolved contradicting raw record lowers the score below 60.
        result, _ = _v18_timeout("edge-api", [
            _pool_line_at("edge-api", "2024-08-01T12:00:10Z"),
            {
                "_time": "2024-08-01T12:00:20Z",
                "service": "edge-api",
                "level": "ERROR",
                "message": "slow query on edge-api took 9000ms",
            },
        ])
        assert result["cause"]["confidence"] < 60
        assert result["cause"]["confidence"] < _ONE_RAW
        assert result["cause"]["confidence"] == 24
        assert "UNKNOWN" in result["cause"]["statement"]

    def test_derived_label_alone_binds_nothing(self):
        # (c) A derived label alone gives 0 cause support and no bound cause.
        evidence = {
            "check_signals": {
                "_receipt_sequence_order": 1,
                "_receipt_tool": "apm_worker",
                "signals": {
                    "anomaly_start": "2024-08-01T12:00:00Z",
                    "anomaly_detected": True,
                    "anomaly_type": "intermittent_errors",
                    "summary": "connection pool exhausted",
                    "golden_signals": {"errors": {"rate": 0.4}},
                },
            }
        }
        assessment = bind_hypothesis(
            Hypothesis(
                name="connection_pool_leak",
                root_cause="connection pool exhaustion in edge-api; intermittent",
                base_score=70,
                evidence_refs=["golden_signals:intermittent"],
                reasoning="the detector said so",
            ),
            incident_type="flapping",
            service="edge-api",
            incident=_v18_incident("edge-api"),
            evidence=evidence,
        )
        assert assessment["cause_confidence"] == 0
        assert assessment["cause_refs"] == []
        assert "UNKNOWN" in assessment["statement"]

    def test_raw_series_scores_and_the_label_does_not(self):
        # (d) The same label plus its raw record scores only the raw record.
        raw = _pool_line_at("edge-api", "2024-08-01T12:00:10Z")
        signals = {
            "_receipt_sequence_order": 1,
            "_receipt_tool": "apm_worker",
            "signals": {
                "anomaly_start": "2024-08-01T12:00:00Z",
                "anomaly_detected": True,
                "anomaly_type": "pool_exhausted",
                "summary": "connection pool exhausted",
                "golden_signals": {"errors": {"rate": 0.4}},
            },
        }
        logs = {
            "_receipt_sequence_order": 2,
            "_receipt_tool": "log_worker",
            "logs": {"results": [raw], "count": 1},
        }
        incident = _v18_incident("edge-api")
        hyp = Hypothesis(
            name="connection_pool_leak",
            root_cause="connection pool exhaustion in edge-api",
            base_score=70,
            evidence_refs=["logs:pool_exhaustion", "golden_signals:pool_exhausted"],
            reasoning="template",
        )
        raw_only = bind_hypothesis(
            hyp, incident_type="flapping", service="edge-api",
            incident=incident, evidence={"search_logs": logs},
        )
        both = bind_hypothesis(
            hyp, incident_type="flapping", service="edge-api",
            incident=incident,
            evidence={"check_signals": signals, "search_logs": logs},
        )
        assert raw_only["cause_confidence"] == _ONE_RAW
        assert both["cause_confidence"] == raw_only["cause_confidence"]
        assert both["cause_refs"]
        assert _classes(both["cause_refs"]) <= {"raw", "derived"}
        assert "raw" in _classes(both["cause_refs"])
        for ref in both["cause_refs"]:
            assert ref.get("evidence_class") != "derived"

    def test_out_of_window_record_adds_nothing(self):
        # (e) A record outside the alignment window adds 0 and is not cited.
        inside = _pool_line_at("edge-api", "2024-08-01T12:00:10Z")
        outside = _pool_line_at("edge-api", "2024-01-01T00:00:00Z")
        one, _ = _v18_timeout("edge-api", [inside])
        both, _ = _v18_timeout("edge-api", [inside, outside])
        assert one["cause"]["confidence"] == both["cause"]["confidence"] == _ONE_RAW
        cited = [ref.get("timestamp") for ref in both["cause"]["evidence_refs"]]
        assert "2024-01-01T00:00:00Z" not in cited
        assert cited == ["2024-08-01T12:00:10Z"]

    def test_pattern_word_without_a_series_is_an_unknown(self):
        # (f) No cited raw series: the pattern word leaves the cause.
        evidence = {
            "search_logs": {
                "_receipt_sequence_order": 2,
                "_receipt_tool": "log_worker",
                "logs": {"results": [
                    _pool_line_at("edge-api", "2024-08-01T12:00:10Z"),
                ], "count": 1},
            },
            "check_signals": {
                "_receipt_sequence_order": 1,
                "_receipt_tool": "apm_worker",
                "signals": {
                    "anomaly_start": "2024-08-01T12:00:00Z",
                    "anomaly_detected": True,
                    "anomaly_type": "intermittent_errors",
                    "pattern": "sawtooth",
                    "golden_signals": {"errors": {"rate": 0.2}},
                },
            },
        }
        assessment = bind_hypothesis(
            Hypothesis(
                name="connection_pool_leak",
                root_cause="connection pool exhaustion in edge-api; intermittent",
                base_score=70,
                evidence_refs=["logs:pool_exhaustion", "golden_signals:intermittent"],
                reasoning="template",
            ),
            incident_type="flapping",
            service="edge-api",
            incident=_v18_incident("edge-api"),
            evidence=evidence,
        )
        assert "intermittent" not in assessment["statement"].lower()
        assert any("intermittent" in item for item in assessment["unknowns"])
        assert assessment["cause_confidence"] == _ONE_RAW

    def test_limit_reached_binds_pool_exhaustion(self):
        # (g) "limit … reached" is pool exhaustion, including a timeout on the same line.
        result, _ = _v18_timeout("edge-api", [
            _pool_line_at(
                "edge-api",
                "2024-08-01T12:00:10Z",
                "pool size limit reached and the connection timed out",
            ),
        ])
        statement = result["cause"]["statement"]
        assert statement == "connection pool exhausted on edge-api"
        assert result["cause"]["confidence"] == _ONE_RAW
        assert result["cause"]["category"] == "connection_pool_exhaustion"
        assert _DOWNSTREAM_UNKNOWN in result["cause"]["unknowns"]
        assert not re.search(r"\d", statement)
        rate, _ = _v18_timeout("edge-api", [
            _pool_line_at(
                "edge-api",
                "2024-08-01T12:00:10Z",
                "rate limit reached and the connection timed out",
            ),
        ])
        assert rate["cause"]["category"] != "connection_pool_exhaustion"

    def test_latency_slow_query_binds(self):
        # (h) A latency incident binds a supported slow query, not the derived label.
        evidence = {
            "search_logs": {
                "_receipt_sequence_order": 2,
                "_receipt_tool": "log_worker",
                "logs": {"results": [
                    {
                        "_time": "2024-08-01T12:00:05Z",
                        "service": "catalog-store",
                        "level": "WARN",
                        "message": "catalog-store rebalancing started",
                    },
                    {
                        "_time": "2024-08-01T12:00:20Z",
                        "service": "catalog-store",
                        "level": "ERROR",
                        "message": "slow query on catalog-store took 8000ms",
                    },
                ], "count": 2},
            },
            "check_signals": {
                "_receipt_sequence_order": 1,
                "_receipt_tool": "apm_worker",
                "signals": {
                    "anomaly_start": "2024-08-01T12:00:00Z",
                    "anomaly_detected": True,
                    "anomaly_type": "latency_spike",
                    "summary": "latency elevated",
                    "golden_signals": {
                        "latency": {"p95": 9000, "baseline_p95": 100},
                    },
                },
            },
        }
        incident = _v18_incident("query-api")
        sup = SentinalAISupervisor()
        sup._tls.current_incident = dict(incident)
        sup._tls.last_evidence = evidence
        result = sup._analyze_evidence("INC-V18", dict(incident), "latency", evidence)
        statement = result["cause"]["statement"].lower()
        assert "slow quer" in statement
        assert result["cause"]["confidence"] == _ONE_RAW
        assert result["cause"]["category"] == "slow_queries"
        assert _classes(result["cause"]["evidence_refs"]) == {"raw"}
        assert "latency_spike" not in statement


def _v18_with_evidence(service, evidence, incident_type="timeout"):
    incident = _v18_incident(service)
    sup = SentinalAISupervisor()
    sup._tls.current_incident = dict(incident)
    sup._tls.last_evidence = evidence
    return sup._analyze_evidence("INC-V18", dict(incident), incident_type, evidence)


class TestToolErrorIsUnchecked:
    """A tool error is a search that did not happen.

    Expected directions were written before this class ran.
    (a) The logs search errors alone. Coverage records the tool and the
        error. The cause is UNKNOWN. The statement does not say the search
        found nothing.
    (b) Metrics error, and a raw pool line from the log tool. The cause
        binds from that line at the one-record score. The metrics tool is
        unchecked and is not a contradiction.
    """

    def test_log_search_error_stays_unknown(self):
        evidence = {
            "search_timeout_logs": {
                "_receipt_sequence_order": 2,
                "_receipt_tool": "log_worker",
                "tool": "splunk.search_oneshot",
                "tool_status": "error",
                "error": "gateway_exception: connection reset",
            }
        }
        result = _v18_with_evidence("edge-api", evidence)
        cause = result["cause"]
        assert "UNKNOWN" in cause["statement"]
        assert cause["confidence"] < 60
        assert cause["evidence_refs"] == []
        assert cause["contradictions"] == []
        statement = cause["statement"].lower()
        assert "nothing" not in statement
        assert "empty" not in statement
        assert "found" not in statement
        assert any(
            "splunk.search_oneshot" in item and "gateway_exception" in item
            for item in cause["unknowns"]
        )
        assert not any("no in-window mechanism" in item for item in cause["unknowns"])
        from supervisor.helpers.cause_binding import (
            build_evidence_snapshot,
            unchecked_coverage,
        )
        coverage = unchecked_coverage(_v18_incident("edge-api"), evidence)
        assert coverage["tool_errors"] == [{
            "evidence_key": "search_timeout_logs",
            "tool": "splunk.search_oneshot",
            "error": "gateway_exception: connection reset",
        }]
        assert "search_timeout_logs" not in build_evidence_snapshot(evidence)

    def test_raw_evidence_binds_when_another_tool_errors(self):
        evidence = {
            "query_metrics": {
                "_receipt_sequence_order": 4,
                "_receipt_tool": "metrics_worker",
                "tool": "sysdig.query_metrics",
                "tool_status": "error",
                "error": "mcp_exception: timeout",
            },
            "search_timeout_logs": {
                "_receipt_sequence_order": 3,
                "_receipt_tool": "log_worker",
                "logs": {
                    "results": [_pool_line_at("edge-api", "2024-08-01T12:00:10Z")],
                    "count": 1,
                },
            },
        }
        result = _v18_with_evidence("edge-api", evidence)
        cause = result["cause"]
        assert cause["statement"] == "connection pool exhausted on edge-api"
        assert cause["confidence"] == _ONE_RAW
        assert cause["contradictions"] == []
        assert cause["evidence_refs"]
        assert _classes(cause["evidence_refs"]) == {"raw"}
        assert all(
            (ref.get("locator") or {}).get("evidence_key") != "query_metrics"
            for ref in cause["evidence_refs"]
        )
        from supervisor.helpers.cause_binding import (
            build_evidence_snapshot,
            unchecked_coverage,
        )
        coverage = unchecked_coverage(_v18_incident("edge-api"), evidence)
        assert coverage["tool_errors"] == [{
            "evidence_key": "query_metrics",
            "tool": "sysdig.query_metrics",
            "error": "mcp_exception: timeout",
        }]
        snapshot = build_evidence_snapshot(evidence)
        assert "query_metrics" not in snapshot
        assert "search_timeout_logs" in snapshot


# A pool gauge with active well below max and many idle. The conflict
# score is 40 + (-8) for the pool line + (-8) for the reading.
_POOL_CONFLICT = 40 + (-8) + (-8)
_UNSATURATED = "pool not saturated: active 2, idle 40, max 50"
_POOL_WHEN = "2024-08-01T12:00:10Z"


def _pool_candidate_evidence(metric_payload):
    return {
        "search_timeout_logs": {
            "_receipt_sequence_order": 2,
            "_receipt_tool": "log_worker",
            "logs": {
                "results": [_pool_line_at("edge-api", _POOL_WHEN)],
                "count": 1,
            },
        },
        "query_pool": metric_payload,
    }


def _flat_pool_series():
    """Prometheus-style list of points. Same numbers as the signals object."""
    return {
        "_receipt_sequence_order": 5,
        "_receipt_tool": "metrics_worker",
        "intent": "resource",
        "metrics": [
            {"name": "db_connection_pool_active", "timestamp": _POOL_WHEN, "value": 2},
            {"name": "db_connection_pool_idle", "timestamp": _POOL_WHEN, "value": 40},
            {"name": "db_connection_pool_max", "timestamp": _POOL_WHEN, "value": 50},
        ],
    }


def _named_pool_series():
    """Prometheus dict of series. pool_max lives on the active series."""
    return {
        "_receipt_sequence_order": 5,
        "_receipt_tool": "metrics_worker",
        "metrics": {
            "db_connection_pool_active": {
                "values": [{"timestamp": _POOL_WHEN, "value": 2}],
                "pool_max": 50,
            },
            "db_connection_pool_idle": {
                "values": [{"timestamp": _POOL_WHEN, "value": 40}],
            },
        },
    }


def _signals_pool():
    """APM / golden-signals payload. The gauge is nested under signals."""
    return {
        "_receipt_sequence_order": 5,
        "_receipt_tool": "apm_worker",
        "signals": {
            "anomaly_start": _POOL_WHEN,
            "golden_signals": {
                "latency": {"p95": 120, "baseline_p95": 80},
            },
            "db_connection_pool": {"active": 2, "idle": 40, "max": 50},
        },
    }


def _apm_pool():
    """APM object stored as the tool result. max_pool_size is the limit."""
    return {
        "_receipt_sequence_order": 5,
        "_receipt_tool": "apm_worker",
        "service": "edge-api",
        "timestamp": _POOL_WHEN,
        "db_connection_pool": {
            "pool_name": "HikariPool-1",
            "max_pool_size": 50,
            "active": 2,
            "idle": 40,
            "pending": 0,
        },
    }


def _contradiction_outcome(result):
    cause = result["cause"]
    return (
        cause["statement"],
        cause["confidence"],
        cause["category"],
        tuple(item["statement"] for item in cause["contradictions"]),
        tuple(cause["evidence_refs"]),
    )


class TestUnsaturatedPoolContradicts:
    """A pool metric that is not saturated contradicts exhaustion.

    Expected before the run, for every payload shape:
    statement ``timeout observed; cause UNKNOWN``, confidence 24
    (below 60), no cause refs, and contradictions include
    ``pool not saturated: active 2, idle 40, max 50``.
    The flat series, the named series, the signals object, and the
    APM object produce that same outcome.
    """

    def test_same_reading_contradicts_in_every_shape(self):
        outcomes = [
            _contradiction_outcome(_v18_with_evidence(
                "edge-api", _pool_candidate_evidence(payload),
            ))
            for payload in (
                _flat_pool_series(),
                _named_pool_series(),
                _signals_pool(),
                _apm_pool(),
            )
        ]
        expect = (
            "timeout observed; cause UNKNOWN",
            _POOL_CONFLICT,
            "unknown",
            ("connection pool exhaustion", _UNSATURATED),
            (),
        )
        assert all(item == expect for item in outcomes)
        assert outcomes[0] == outcomes[1] == outcomes[2] == outcomes[3]
        assert outcomes[0][1] < 60

    def test_single_object_golden_signals_are_consulted(self):
        # Expected before the run. The pool log plus a single-object APM
        # payload (active 2, idle 40, max 50) does not bind pool exhaustion
        # above 60. The contradiction names the unsaturated reading. The
        # check_golden_signals snapshot records are non-empty, and the
        # receipt does not say the call returned nothing. The same
        # contradiction is recorded when that gauge is under signals.
        from supervisor.helpers.cause_binding import build_evidence_snapshot
        from supervisor.receipt import ReceiptCollector

        evidence = _pool_candidate_evidence(_apm_pool())
        evidence["check_golden_signals"] = evidence.pop("query_pool")
        result = _v18_with_evidence("edge-api", evidence)
        cause = result["cause"]
        assert cause["confidence"] < 60
        assert cause["confidence"] == _POOL_CONFLICT
        assert "UNKNOWN" in cause["statement"]
        assert cause["category"] != "connection_pool_exhaustion"
        assert any(
            item["statement"] == _UNSATURATED for item in cause["contradictions"]
        )
        snap = build_evidence_snapshot(evidence)
        assert snap["check_golden_signals"]["records"]

        collector = ReceiptCollector(case_id="INC-V18")
        receipt = collector.start(
            "dynatrace.get_metrics", "get_golden_signals", {"service": "edge-api"},
        )
        collector.finish(receipt, _apm_pool())
        assert receipt.missing_reason != "no_evidence_returned"
        assert receipt.result_count >= 1
        assert receipt.consulted

        wrapped = _pool_candidate_evidence(_signals_pool())
        wrapped["check_golden_signals"] = wrapped.pop("query_pool")
        wrapped_result = _v18_with_evidence("edge-api", wrapped)
        wrapped_cause = wrapped_result["cause"]
        assert wrapped_cause["confidence"] < 60
        assert wrapped_cause["confidence"] == _POOL_CONFLICT
        assert any(
            item["statement"] == _UNSATURATED
            for item in wrapped_cause["contradictions"]
        )
        wrapped_snap = build_evidence_snapshot(wrapped)
        assert wrapped_snap["check_golden_signals"]["records"]


def _failed_log_search():
    """Log search errored. A signal was retrieved and reported no window."""
    return {
        "search_logs": {
            "_receipt_sequence_order": 2,
            "_receipt_tool": "log_worker",
            "tool": "splunk.search_oneshot",
            "tool_status": "error",
            "error": "gateway_exception: connection reset",
        },
        "check_signals": {
            "_receipt_sequence_order": 1,
            "_receipt_tool": "apm_worker",
            "signals": {
                "anomaly_start": "2024-08-01T12:00:00Z",
                "golden_signals": {
                    "latency": {"p95": 900, "baseline_p95": 80},
                    "errors": {"rate": 0.4, "baseline_rate": 0.01},
                },
            },
        },
    }


_FAILED_LOG = {
    "evidence_key": "search_logs",
    "tool": "splunk.search_oneshot",
    "error": "gateway_exception: connection reset",
}


class TestFailedSearchOnEveryPath:
    """Latency and error_spike name a failed log search.

    Expected before the run: ``unchecked_coverage.tool_errors`` is
    exactly the splunk search and its gateway error. The cause
    unknowns include ``search did not happen: splunk.search_oneshot:
    gateway_exception: connection reset``. The error is not a
    contradiction and not a cause ref.
    """

    def _assert_failed_log(self, incident_type):
        result = _v18_with_evidence(
            "edge-api", _failed_log_search(), incident_type=incident_type,
        )
        cause = result["cause"]
        coverage = result["_confidence_provenance"]["unchecked_coverage"]
        assert coverage["tool_errors"] == [_FAILED_LOG]
        assert (
            "search did not happen: splunk.search_oneshot: "
            "gateway_exception: connection reset"
        ) in cause["unknowns"]
        assert cause["contradictions"] == []
        assert all(
            (ref.get("locator") or {}).get("evidence_key") != "search_logs"
            for ref in cause["evidence_refs"]
        )
        assert "UNKNOWN" in cause["statement"]
        assert cause["confidence"] < 60

    def test_latency_log_search_error_is_unchecked(self):
        self._assert_failed_log("latency")

    def test_error_spike_log_search_error_is_unchecked(self):
        self._assert_failed_log("error_spike")


def _pool_signals(result):
    """Signals that cite a pool, on the cause or on a contradiction."""
    cause = result["cause"]
    signals = [
        ref.get("signal")
        for ref in cause.get("evidence_refs") or []
        if "pool" in str(ref.get("signal") or "")
    ]
    for item in cause.get("contradictions") or []:
        if "pool" in str(item.get("statement") or "").lower():
            signals.append("contradiction:" + item["statement"])
        for ref in item.get("evidence_refs") or []:
            if "pool" in str(ref.get("signal") or ""):
                signals.append(ref.get("signal"))
    return signals


def _assert_not_a_pool(result):
    cause = result["cause"]
    assert "pool" not in cause["statement"].lower()
    assert cause["category"] != "connection_pool_exhaustion"
    assert _pool_signals(result) == []


class TestPoolWordingNamesAPool:
    """v1.10. A limit is pool exhaustion only when the line names a pool.

    Expected before the run, written against 0f527ba:
    generic resource, rate, capacity, and slot limits produce no pool
    statement and no pool ref. An identifier-named pool
    (CamelCase ``...Pool`` or a lowercase token ending in ``pool``)
    still binds when other words sit between ``limit`` and ``reached``.
    The standalone word ``pool`` on ``pool size limit reached`` still binds.
    """

    def test_generic_limits_are_not_pool_exhaustion(self):
        lines = (
            "container cpu resource limit exceeded",
            "container memory resource limit exceeded",
            "connection rate limit reached",
            "capacity limit reached",
            "replication slot limit reached",
        )
        for message in lines:
            result, _ = _v18_timeout("edge-api", [
                _pool_line_at("edge-api", "2024-08-01T12:00:10Z", message),
            ])
            _assert_not_a_pool(result)

    def test_identifier_pool_limit_with_words_between(self):
        # CamelCase class ending in Pool; overflow and size sit between
        # limit and reached. Lowercase token ending in pool, same shape.
        for message in (
            "HikariPool limit overflow size reached",
            "dbpool limit overflow size reached",
        ):
            result, _ = _v18_timeout("edge-api", [
                _pool_line_at("edge-api", "2024-08-01T12:00:10Z", message),
            ])
            cause = result["cause"]
            assert cause["statement"] == "connection pool exhausted on edge-api"
            assert cause["confidence"] == _ONE_RAW
            assert cause["category"] == "connection_pool_exhaustion"
            assert {ref["signal"] for ref in cause["evidence_refs"]} == {
                "connection_pool_exhausted",
            }

    def test_standalone_pool_limit_reached_still_binds(self):
        result, _ = _v18_timeout("edge-api", [
            _pool_line_at(
                "edge-api",
                "2024-08-01T12:00:10Z",
                "pool size limit reached and the connection timed out",
            ),
        ])
        cause = result["cause"]
        assert cause["statement"] == "connection pool exhausted on edge-api"
        assert cause["confidence"] == _ONE_RAW
        assert cause["category"] == "connection_pool_exhaustion"
        assert {ref["signal"] for ref in cause["evidence_refs"]} == {
            "connection_pool_exhausted",
        }


class TestConnectionPoolNotAnyPool:
    """v1.11. Only a connection pool binds as connection-pool exhaustion.

    Expected before the run, written against 2505022. Each line below
    produces no connection-pool statement and no pool ref. The v1.10
    connection-pool lines and the generic limit lines stay as they are.
    A saturation thread-pool proposal stays a thread pool.
    """

    def test_non_connection_pools_are_not_connection_pool_exhaustion(self):
        lines = (
            "ThreadPool limit of 200 reached",
            "thread pool limit reached",
            "worker pool overflow",
            "ForkJoinPool queue limit reached",
            "spool limit reached",
            "bufferpool limit reached",
        )
        for message in lines:
            result, _ = _v18_timeout("edge-api", [
                _pool_line_at("edge-api", "2024-08-01T12:00:10Z", message),
            ])
            _assert_not_a_pool(result)
