"""Underclaim and overclaim when every search returns the service's logs.

Expected answers are fixed in EXPECTED before any investigation runs.
The strings are not taken from the OSS seed.
"""
from __future__ import annotations

import os
import re

from supervisor.agent import SentinalAISupervisor
from supervisor.evidence_citation import annotate_citations
from supervisor.guardrails import CircuitBreakerRegistry, ExecutionBudget
from supervisor.helpers.cause_binding import build_evidence_snapshot
from supervisor.helpers.timeout_evidence import resolve_evidence_ref
from supervisor.receipt import ReceiptCollector
from supervisor.tool_selector import INCIDENT_PLAYBOOKS

# Answers written before the runs.
EXPECTED = {
    "latency_slow": {
        "incident_type": "latency",
        "service": "checkout-api",
        "statement": "slow queries on catalog-index",
        "confidence": 62,
        "cause_service": "catalog-index",
        "symptom_service": "checkout-api",
        "symptom_signal": "error_observed",
        "introduced": False,
    },
    "error_spike_exception": {
        "incident_type": "error_spike",
        "service": "billing-api",
        "statement": "IllegalStateException in billing-api",
        "confidence": 62,
        "cause_service": "billing-api",
        "symptom_service": "billing-api",
        "symptom_signal": "error_observed",
        "introduced": False,
    },
    "deploy_version": {
        "incident_type": "error_spike",
        "service": "billing-api",
        "statement": (
            "IllegalStateException in billing-api 4.8.2, "
            "deployed at 2024-11-04T07:50:00Z"
        ),
        # The exception line and the deploy record are two agreeing raw refs.
        # 20 + 22 + 20 + 8.
        "confidence": 70,
        "cause_service": "billing-api",
        "symptom_service": "billing-api",
        "symptom_signal": "error_observed",
        "introduced": False,
        "unknown_fragment": "whether the deploy introduced the error",
    },
    "timeout_pool": {
        "incident_type": "timeout",
        "service": "order-api",
        "statement": "connection pool for inventory-db exhausted",
        "confidence": 62,
        "cause_service": "inventory-db",
        "symptom_service": "order-api",
        "symptom_signal": "timeout_observed",
        "introduced": False,
    },
    "no_mechanism": {
        "incident_type": "error_spike",
        "service": "checkout-api",
        "statement": "error_spike observed; cause UNKNOWN",
        "confidence_below": 60,
        "symptom_service": "checkout-api",
        "symptom_signal": "error_observed",
        "forbidden": ("disk", "pool", "IllegalState"),
    },
    "conflict": {
        "incident_type": "timeout",
        "service": "order-api",
        "statement": "timeout observed; cause UNKNOWN",
        "confidence_below": 60,
        "contradictions": 2,
    },
    "summary_only": {
        "incident_type": "timeout",
        "service": "order-api",
        "statement": "timeout observed; cause UNKNOWN",
        "confidence_below": 60,
        "forbidden": ("pool", "exhausted"),
    },
    # Written before the runs. Not taken from the OSS seed.
    "owner_latency": {
        "incident_type": "latency",
        "service": "fulfillment-api",
        "statement": "fulfillment-api's connection pool to ledger-db exhausted",
        "confidence": 62,
        "caller": "fulfillment-api",
        "downstream": "ledger-db",
        "unknown_fragment": "why ledger-db refuses connections",
    },
    "owner_timeout_thin": {
        "incident_type": "timeout",
        "service": "fulfillment-api",
        "statement": "fulfillment-api's connection pool to ledger-db exhausted",
        "confidence": 62,
        "caller": "fulfillment-api",
        "downstream": "ledger-db",
        "unknown_fragment": "why ledger-db refuses connections",
    },
    "pool_over_exception": {
        "incident_type": "error_spike",
        "service": "invoicing-api",
        "statement": "invoicing-api's connection pool to invoice-store exhausted",
        "confidence": 62,
        "caller": "invoicing-api",
        "downstream": "invoice-store",
        "absent": ("SocketTimeoutException", "20", "slots"),
    },
    "bare_timeout_exception": {
        "incident_type": "latency",
        "service": "catalog-api",
        "statement": "latency observed; cause UNKNOWN",
        "confidence": 12,
        "symptom_statement": "timeout observed",
        "symptom_service": "catalog-api",
        "symptom_signal": "timeout_observed",
        "absent": ("TimeoutException",),
    },
    "latency_timed_out": {
        "incident_type": "latency",
        "service": "dispatch-api",
        "statement": "latency observed; cause UNKNOWN",
        "confidence": 12,
        "symptom_statement": "timeout observed",
        "symptom_service": "dispatch-api",
        "symptom_signal": "timeout_observed",
        "timed_message": "the call timed out",
        "other_message": "health check passed",
    },
}

# The latency hint before this change. It does not match a line that
# only says "timed out".
_LATENCY_HINT_BEFORE = "latency OR slow {service}"

_START = "2024-11-04T08:00:00Z"


class _AllLogs:
    """Fake gateway. Every search returns the case corpus. The query is ignored."""

    def __init__(self, records, changes=None, summary=""):
        self.records = [dict(row) for row in records]
        self.changes = [dict(row) for row in (changes or [])]
        self.summary = summary
        self.queries: list[str] = []

    def execute(self, action, params):
        params = params or {}
        if action == "search_logs" or str(action).startswith("search_"):
            self.queries.append(str(params.get("query") or ""))
            return {
                "summary": self.summary,
                "logs": {
                    "results": [dict(row) for row in self.records],
                    "count": len(self.records),
                },
            }
        if action in ("get_change_data", "get_change_records"):
            return {"changes": [dict(row) for row in self.changes]}
        if action in ("get_golden_signals", "check_latency"):
            return {"signals": {"golden_signals": {}}}
        if action == "get_events":
            return {"events": []}
        if action in ("query_metrics", "get_resource_metrics"):
            return {"metrics": {"metrics": []}}
        return {}


def _investigate(incident_type, service, records, changes=None, summary=""):
    saved = {
        key: os.environ.get(key)
        for key in ("LLM_ENABLED", "PARALLEL_PLAYBOOK", "CALIBRATION_ENABLED")
    }
    os.environ["LLM_ENABLED"] = "false"
    os.environ["PARALLEL_PLAYBOOK"] = "false"
    os.environ["CALIBRATION_ENABLED"] = "false"
    try:
        return _investigate_with_flags(incident_type, service, records, changes, summary)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _investigate_with_flags(incident_type, service, records, changes=None, summary=""):
    sup = SentinalAISupervisor()
    sup._parallel_playbook = False
    gateway = _AllLogs(records, changes, summary=summary)
    for name in list(sup.workers):
        sup.workers[name] = gateway
    incident = {
        "incident_id": "INC-FULL",
        "affected_service": service,
        "summary": summary or f"{service} {incident_type}",
        "start_time": _START,
    }
    sup._tls.current_incident = dict(incident)
    sup._tls.run_started = "2024-11-04T12:00:00Z"
    receipts = ReceiptCollector(case_id="INC-FULL")
    evidence = sup._execute_playbook(
        incident_type, "INC-FULL", service, receipts,
        ExecutionBudget(), CircuitBreakerRegistry(),
    )
    sup._tls.last_evidence = evidence
    result = sup._analyze_evidence("INC-FULL", dict(incident), incident_type, evidence)
    annotate_citations(result, evidence)
    result["receipts"] = receipts.to_list()
    return result, gateway, evidence


def _assert_supported(result, gateway, evidence, expected):
    cause = result["cause"]
    assert cause["statement"] == expected["statement"], cause["statement"]
    assert result["root_cause"] == expected["statement"]
    assert cause["confidence"] == expected["confidence"]
    assert result["confidence"] == expected["confidence"]
    assert "introduced" not in cause["statement"].lower()
    assert "caused" not in cause["statement"].lower()
    services = {ref["service"] for ref in cause["evidence_refs"]}
    assert expected["cause_service"] in services, cause["evidence_refs"]
    symptom_refs = result["symptom"]["evidence_refs"]
    assert symptom_refs, result["symptom"]
    assert any(ref.get("signal") == expected["symptom_signal"] for ref in symptom_refs)
    assert any(ref.get("service") == expected["symptom_service"] for ref in symptom_refs)
    if expected.get("unknown_fragment"):
        assert any(expected["unknown_fragment"] in item for item in cause["unknowns"])
    _assert_full_return(result, gateway, evidence)


def _assert_full_return(result, gateway, evidence):
    assert gateway.queries, "the playbook did not search"
    # A line that shares no token with the query is still returned.
    for key, val in evidence.items():
        if not isinstance(val, dict):
            continue
        results = (val.get("logs") or {}).get("results") if isinstance(val.get("logs"), dict) else None
        if isinstance(results, list) and results:
            assert len(results) == len(gateway.records)
    searches = [
        r for r in result["receipts"]
        if r.get("action") == "search_logs" and r.get("status") == "success"
    ]
    assert searches
    for receipt in searches:
        assert receipt["result_count"] == len(gateway.records)
        assert len(receipt["consulted"]) == len(gateway.records)
    snap = build_evidence_snapshot(evidence)
    hashed = [
        rec["content_hash"]
        for entry in snap.values()
        if isinstance(entry, dict)
        for rec in entry["records"]
    ]
    consulted = [item["content_hash"] for item in searches[0]["consulted"]]
    assert consulted
    assert set(consulted) <= set(hashed)
    coverage = result["_confidence_provenance"]["unchecked_coverage"]
    assert "requested_window" in coverage
    assert "truncations" in coverage
    assert "after_run_start" in coverage
    # This gateway does not send truncated. A full list is not complete.
    assert coverage["truncation_unknown"] is True
    assert coverage["truncations"]
    assert all(row["truncation_unknown"] is True for row in coverage["truncations"])


class TestUnderclaimFullLogs:
    def test_latency_names_slow_queries(self):
        records = [
            {
                "_time": "2024-11-04T07:59:10Z",
                "service": "checkout-api",
                "level": "ERROR",
                "message": "ERROR checkout-api request failed",
            },
            {
                "_time": "2024-11-04T07:59:40Z",
                "service": "catalog-index",
                "level": "ERROR",
                "message": "slow query on catalog-index took 4200ms",
            },
            {
                "_time": "2024-11-04T07:59:50Z",
                "service": "catalog-index",
                "level": "INFO",
                "message": "INFO catalog-index checkpoint",
            },
        ]
        result, gateway, evidence = _investigate("latency", "checkout-api", records)
        _assert_supported(result, gateway, evidence, EXPECTED["latency_slow"])
        joined = " ".join(gateway.queries).lower()
        assert "checkpoint" not in joined
        blobs = []
        for val in evidence.values():
            if isinstance(val, dict) and isinstance(val.get("logs"), dict):
                blobs.extend(row.get("message", "") for row in val["logs"]["results"])
        assert any("checkpoint" in msg for msg in blobs)

    def test_error_spike_names_the_exception(self):
        records = [{
            "_time": "2024-11-04T07:59:20Z",
            "service": "billing-api",
            "level": "ERROR",
            "message": "IllegalStateException while charging card",
        }]
        result, gateway, evidence = _investigate("error_spike", "billing-api", records)
        _assert_supported(result, gateway, evidence, EXPECTED["error_spike_exception"])

    def test_deploy_states_version_and_time_not_causation(self):
        records = [{
            "_time": "2024-11-04T07:59:20Z",
            "service": "billing-api",
            "level": "ERROR",
            "message": "IllegalStateException in billing-api 4.8.2 while charging card",
        }]
        changes = [{
            "change_type": "deployment",
            "service": "billing-api",
            "description": "release 4.8.2 of billing-api",
            "scheduled_start": "2024-11-04T07:50:00Z",
        }]
        result, gateway, evidence = _investigate(
            "error_spike", "billing-api", records, changes,
        )
        _assert_supported(result, gateway, evidence, EXPECTED["deploy_version"])
        deploy_refs = [
            ref for ref in result["cause"]["evidence_refs"]
            if ref.get("signal") == "deployment"
        ]
        assert deploy_refs
        assert deploy_refs[0]["service"] == "billing-api"

    def test_timeout_keeps_each_records_service(self):
        records = [
            {
                "_time": "2024-11-04T07:59:30Z",
                "service": "order-api",
                "level": "ERROR",
                "message": "ERROR timeout waiting for connection: inventory-db",
            },
            {
                "_time": "2024-11-04T07:59:20Z",
                "service": "inventory-db",
                "level": "ERROR",
                "message": "ERROR connection pool exhausted: 20/20 connections in use",
            },
        ]
        result, gateway, evidence = _investigate("timeout", "order-api", records)
        _assert_supported(result, gateway, evidence, EXPECTED["timeout_pool"])
        pool_refs = [
            ref for ref in result["cause"]["evidence_refs"]
            if ref.get("signal") == "connection_pool_exhausted"
        ]
        assert pool_refs
        assert pool_refs[0]["service"] == "inventory-db"
        assert pool_refs[0]["service"] != "order-api"


class TestOverclaimFullLogs:
    def test_error_without_a_mechanism_stays_unknown(self):
        records = [{
            "_time": "2024-11-04T07:59:10Z",
            "service": "checkout-api",
            "level": "ERROR",
            "message": "ERROR upstream reset",
        }]
        result, gateway, evidence = _investigate("error_spike", "checkout-api", records)
        expected = EXPECTED["no_mechanism"]
        assert result["cause"]["statement"] == expected["statement"]
        assert result["confidence"] < expected["confidence_below"]
        for word in expected["forbidden"]:
            assert word.lower() not in result["root_cause"].lower()
        refs = result["symptom"]["evidence_refs"]
        assert any(
            ref.get("signal") == expected["symptom_signal"]
            and ref.get("service") == expected["symptom_service"]
            for ref in refs
        )
        _assert_full_return(result, gateway, evidence)

    def test_two_mechanisms_stay_unknown(self):
        records = [
            {
                "_time": "2024-11-04T07:59:30Z",
                "service": "order-api",
                "level": "ERROR",
                "message": "ERROR timeout waiting for connection: inventory-db",
            },
            {
                "_time": "2024-11-04T07:59:20Z",
                "service": "inventory-db",
                "level": "ERROR",
                "message": "ERROR connection pool exhausted: 20/20 connections in use",
            },
            {
                "_time": "2024-11-04T07:59:25Z",
                "service": "inventory-db",
                "level": "ERROR",
                "message": "slow query on inventory-db took 8000ms",
            },
        ]
        result, _gateway, _evidence = _investigate("timeout", "order-api", records)
        expected = EXPECTED["conflict"]
        assert result["cause"]["statement"] == expected["statement"]
        assert result["confidence"] < expected["confidence_below"]
        assert len(result["cause"]["contradictions"]) == expected["contradictions"]

    def test_summary_wording_is_not_a_cause(self):
        records = [{
            "_time": "2024-11-04T07:59:30Z",
            "service": "order-api",
            "level": "INFO",
            "message": "INFO healthcheck ok",
        }]
        result, _gateway, _evidence = _investigate(
            "timeout", "order-api", records,
            summary="connection pool exhausted on inventory-db",
        )
        expected = EXPECTED["summary_only"]
        assert result["cause"]["statement"] == expected["statement"]
        assert result["confidence"] < expected["confidence_below"]
        for word in expected["forbidden"]:
            assert word not in result["root_cause"].lower()


def _caller_pool_record():
    """One caller log. The database is the record's downstream field."""
    return {
        "_time": "2024-11-04T07:59:20Z",
        "service": "fulfillment-api",
        "downstream": "ledger-db",
        "level": "ERROR",
        "message": "connection pool exhausted",
    }


def _hint_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_-]+", text)


def _hint_matches(hint: str, message: str) -> bool:
    """Gateway rule: each OR alternative is at most 3 words, any order, case-insensitive."""
    message_words = {word.lower() for word in _hint_words(message)}
    matched = False
    for part in re.split(r"\s+OR\s+", hint.strip(), flags=re.I):
        words = [word.lower() for word in _hint_words(part)]
        assert 0 < len(words) <= 3, hint
        if all(word in message_words for word in words):
            matched = True
    return matched


def _assert_no_unlabeled_numbers(statement: str) -> None:
    scrubbed = re.sub(r"\bv?\d+\.\d+(?:\.\d+)?\b", "", statement)
    scrubbed = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:]+Z", "", scrubbed)
    assert not re.search(r"\d", scrubbed), statement


def _assert_owner(result, evidence, expected) -> None:
    cause = result["cause"]
    statement = cause["statement"]
    caller = expected["caller"]
    downstream = expected["downstream"]
    assert statement == expected["statement"], statement
    assert result["root_cause"] == expected["statement"]
    assert cause["confidence"] == expected["confidence"]
    assert result["confidence"] == expected["confidence"]
    assert statement != f"connection pool exhausted on {caller}"
    assert statement != f"connection pool for {caller} exhausted"
    _assert_no_unlabeled_numbers(statement)
    if expected.get("unknown_fragment"):
        assert any(expected["unknown_fragment"] in item for item in cause["unknowns"])
    refs = cause["evidence_refs"]
    assert refs, cause
    for ref in refs:
        record = resolve_evidence_ref(ref, evidence, result.get("receipts"))
        assert record is not None, ref
        fields = {record.get("service") or "", record.get("downstream") or ""}
        assert caller in fields, (fields, statement)
        assert downstream in fields, (fields, statement)
        for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", statement):
            assert token in fields, (token, fields, statement)
    for word in expected.get("absent") or ():
        assert word not in statement
        assert word not in result["root_cause"]


class TestResourceOwnerAndSymptom:
    """Owner, symptom-as-cause, and latency timeout retrieval.

    Expected statements are the EXPECTED entries above. They were written
    before these investigations ran.
    """

    def test_latency_names_the_downstream_owner(self):
        expected = EXPECTED["owner_latency"]
        records = [
            _caller_pool_record(),
            {
                "_time": "2024-11-04T07:59:40Z",
                "service": "fulfillment-api",
                "level": "INFO",
                "message": "fulfillment checkpoint ok",
            },
        ]
        result, gateway, evidence = _investigate(
            expected["incident_type"], expected["service"], records,
        )
        _assert_owner(result, evidence, expected)
        _assert_full_return(result, gateway, evidence)

    def test_timeout_thin_evidence_names_the_downstream_owner(self):
        expected = EXPECTED["owner_timeout_thin"]
        records = [_caller_pool_record()]
        result, gateway, evidence = _investigate(
            expected["incident_type"], expected["service"], records,
        )
        _assert_owner(result, evidence, expected)
        _assert_full_return(result, gateway, evidence)

    def test_pool_limit_outranks_timeout_exception(self):
        expected = EXPECTED["pool_over_exception"]
        records = [{
            "_time": "2024-11-04T07:59:20Z",
            "service": "invoicing-api",
            "downstream": "invoice-store",
            "level": "ERROR",
            "message": "SocketTimeoutException: connection pool limit reached (slots=20)",
        }]
        result, gateway, evidence = _investigate(
            expected["incident_type"], expected["service"], records,
        )
        _assert_owner(result, evidence, expected)
        _assert_full_return(result, gateway, evidence)

    def test_bare_timeout_exception_stays_unknown(self):
        expected = EXPECTED["bare_timeout_exception"]
        records = [{
            "_time": "2024-11-04T07:59:20Z",
            "service": "catalog-api",
            "level": "ERROR",
            "message": "TimeoutException",
        }]
        result, gateway, evidence = _investigate(
            expected["incident_type"], expected["service"], records,
        )
        cause = result["cause"]
        assert cause["statement"] == expected["statement"], cause["statement"]
        assert result["root_cause"] == expected["statement"]
        assert cause["confidence"] == expected["confidence"]
        assert result["confidence"] == expected["confidence"]
        assert result["symptom"]["statement"] == expected["symptom_statement"]
        refs = result["symptom"]["evidence_refs"]
        assert any(
            ref.get("signal") == expected["symptom_signal"]
            and ref.get("service") == expected["symptom_service"]
            for ref in refs
        )
        for word in expected["absent"]:
            assert word not in cause["statement"]
            assert word not in result["root_cause"]
        _assert_no_unlabeled_numbers(cause["statement"])
        _assert_full_return(result, gateway, evidence)

    def test_latency_retrieves_timed_out_records(self):
        expected = EXPECTED["latency_timed_out"]
        timed = expected["timed_message"]
        other = expected["other_message"]
        records = [
            {
                "_time": "2024-11-04T07:59:20Z",
                "service": "dispatch-api",
                "level": "ERROR",
                "message": timed,
            },
            {
                "_time": "2024-11-04T07:59:30Z",
                "service": "dispatch-api",
                "level": "INFO",
                "message": other,
            },
        ]
        result, gateway, evidence = _investigate(
            expected["incident_type"], expected["service"], records,
        )
        assert result["cause"]["statement"] == expected["statement"], result["cause"]
        assert result["confidence"] == expected["confidence"]
        assert result["symptom"]["statement"] == expected["symptom_statement"]
        assert any(
            ref.get("signal") == expected["symptom_signal"]
            and ref.get("service") == expected["symptom_service"]
            for ref in result["symptom"]["evidence_refs"]
        )
        cited = [
            resolve_evidence_ref(ref, evidence, result.get("receipts"))
            for ref in result["symptom"]["evidence_refs"]
        ]
        assert any(record and record.get("message") == timed for record in cited)
        assert all(not record or record.get("message") != other for record in cited)

        before = _LATENCY_HINT_BEFORE.format(service=expected["service"])
        assert not _hint_matches(before, timed)
        assert any(_hint_matches(query, timed) for query in gateway.queries), gateway.queries
        assert not any(_hint_matches(query, other) for query in gateway.queries), gateway.queries
        hints = [
            step["query_hint"]
            for step in INCIDENT_PLAYBOOKS["latency"]
            if step.get("action") == "search_logs"
        ]
        assert _LATENCY_HINT_BEFORE in hints
        assert "timed out" in hints
        for hint in hints:
            for part in re.split(r"\s+OR\s+", hint, flags=re.I):
                assert 0 < len(_hint_words(part)) <= 3, hint
        returned = []
        for val in evidence.values():
            if isinstance(val, dict) and isinstance(val.get("logs"), dict):
                returned.extend(row.get("message", "") for row in val["logs"]["results"])
        assert timed in returned
        _assert_full_return(result, gateway, evidence)


# v1.10 slow-statement coverage. Expected before the run:
# a database's own duration-in-ms line binds as slow queries on that
# database, and the latency path searches the downstream owner's logs.
_PG_STATEMENT = (
    "duration: 3645.077 ms  statement: SELECT id FROM payments WHERE id = $1"
)
_PG_EXECUTE = (
    "duration: 1820.441 ms  execute <unnamed>: "
    "UPDATE payment_transactions SET status = $1"
)


class _ServiceScopedLogs:
    """Returns a log only when the search names that record's service."""

    def __init__(self, records):
        self.records = [dict(row) for row in records]
        self.services: list[str] = []

    def execute(self, action, params):
        params = params or {}
        if action == "search_logs" or str(action).startswith("search_"):
            service = str(params.get("service") or "")
            self.services.append(service)
            rows = [
                dict(row) for row in self.records
                if str(row.get("service") or "") == service
            ]
            return {"logs": {"results": rows, "count": len(rows)}}
        if action in ("get_change_data", "get_change_records"):
            return {"changes": []}
        if action in ("get_golden_signals", "check_latency"):
            return {"signals": {"golden_signals": {}}}
        if action == "get_events":
            return {"events": []}
        if action in ("query_metrics", "get_resource_metrics"):
            return {"metrics": {"metrics": []}}
        return {}


def _investigate_scoped(incident_type, service, records):
    saved = {
        key: os.environ.get(key)
        for key in ("LLM_ENABLED", "PARALLEL_PLAYBOOK", "CALIBRATION_ENABLED")
    }
    os.environ["LLM_ENABLED"] = "false"
    os.environ["PARALLEL_PLAYBOOK"] = "false"
    os.environ["CALIBRATION_ENABLED"] = "false"
    try:
        sup = SentinalAISupervisor()
        sup._parallel_playbook = False
        gateway = _ServiceScopedLogs(records)
        for name in list(sup.workers):
            sup.workers[name] = gateway
        incident = {
            "incident_id": "INC-FULL",
            "affected_service": service,
            "summary": f"{service} {incident_type}",
            "start_time": _START,
        }
        sup._tls.current_incident = dict(incident)
        sup._tls.run_started = "2024-11-04T12:00:00Z"
        receipts = ReceiptCollector(case_id="INC-FULL")
        evidence = sup._execute_playbook(
            incident_type, "INC-FULL", service, receipts,
            ExecutionBudget(), CircuitBreakerRegistry(),
        )
        sup._tls.last_evidence = evidence
        result = sup._analyze_evidence(
            "INC-FULL", dict(incident), incident_type, evidence,
        )
        return result, gateway
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestDatabaseSlowStatement:
    """Database slow-statement logs and downstream retrieval.

    Expected before the run. A PostgreSQL ``duration: <ms> ms`` line
    followed by ``statement:`` or ``execute`` binds as slow queries on
    the database service, with no application wording. On the latency
    path the playbook searches the alerted service and the downstream
    owner named by the v1.7 ``downstream`` field, and the cause names
    that owner.
    """

    def test_database_duration_statement_binds(self):
        for message in (_PG_STATEMENT, _PG_EXECUTE):
            records = [{
                "_time": "2024-11-04T07:59:40Z",
                "service": "payment-db",
                "level": "LOG",
                "message": message,
            }]
            result, _gateway, _evidence = _investigate(
                "latency", "payment-db", records,
            )
            cause = result["cause"]
            assert cause["statement"] == "slow queries on payment-db"
            assert cause["confidence"] == 62
            assert cause["category"] == "slow_queries"
            assert {ref["service"] for ref in cause["evidence_refs"]} == {"payment-db"}
            assert {ref["signal"] for ref in cause["evidence_refs"]} == {"slow_query"}

    def test_latency_searches_the_downstream_owner(self):
        records = [
            {
                "_time": "2024-11-04T07:59:10Z",
                "service": "checkout-api",
                "downstream": "catalog-db",
                "level": "ERROR",
                "message": "checkout-api latency waiting on catalog-db",
            },
            {
                "_time": "2024-11-04T07:59:40Z",
                "service": "catalog-db",
                "level": "LOG",
                "message": _PG_STATEMENT,
            },
        ]
        result, gateway = _investigate_scoped("latency", "checkout-api", records)
        assert "checkout-api" in gateway.services
        assert "catalog-db" in gateway.services
        cause = result["cause"]
        assert cause["statement"] == "slow queries on catalog-db"
        assert cause["confidence"] == 62
        assert cause["category"] == "slow_queries"
        assert {ref["service"] for ref in cause["evidence_refs"]} == {"catalog-db"}
