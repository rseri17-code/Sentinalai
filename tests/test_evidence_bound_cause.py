"""Evidence-bound timeout causes. Answers are fixed before the run.

Cases:
  (a) pool exhaustion
  (b) supported slow queries
  (c) elevated latency, cause unknown
  (d) missing evidence (a summary that names the pool is not evidence)
  (e) conflicting pool and slow-query records
"""
from __future__ import annotations

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
        assert ref["service"] == "payment-db" or result["cause"]["category"] == "unknown"
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
        assert cause["statement"] == "payment-db latency elevated; cause UNKNOWN"
        assert cause["category"] == "unknown"
        assert cause["confidence"] == 34
        assert cause["confidence"] < 60
        assert result["symptom"]["confidence"] == 85
        assert {r["signal"] for r in cause["evidence_refs"]} == {"latency_elevated"}
        for ref in cause["evidence_refs"]:
            record = resolve_evidence_ref(ref, evidence)
            assert "p95" in record
            assert "pool" not in str(record).lower()
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
        assert pool["confidence"] == slow["confidence"]
        assert pool["confidence"] > elevated["confidence"] > conflict["confidence"] > missing["confidence"]
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
        assert result["confidence"] <= 59
        assert result["cause"]["confidence"] <= 59
        assert "disk full" not in result["root_cause"].lower()
        assert "UNKNOWN" in result["cause"]["statement"]

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
