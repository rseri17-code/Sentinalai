"""Underclaim and overclaim when every search returns the service's logs.

Expected answers are fixed in EXPECTED before any investigation runs.
The strings are not taken from the OSS seed.
"""
from __future__ import annotations

import concurrent.futures
import copy
import os
import re
import threading
import time

import pytest

from supervisor.agent import SentinalAISupervisor
from supervisor.evidence_citation import annotate_citations
from supervisor.guardrails import CircuitBreakerRegistry, ExecutionBudget
from supervisor.helpers.cause_binding import build_evidence_snapshot
from supervisor.helpers.timeout_evidence import resolve_evidence_ref
from supervisor.receipt import ReceiptCollector
from supervisor.strategy_evolver import should_skip_step
from supervisor.tool_selector import INCIDENT_PLAYBOOKS, get_evolved_playbook

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
                "downstream": "inventory-db",
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
                "downstream": "inventory-db",
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


def _cited_timeout(service, downstream):
    """A failure record. The pool line does not establish the dependency."""
    return {
        "_time": "2024-11-04T07:59:10Z",
        "service": service,
        "downstream": downstream,
        "level": "ERROR",
        "message": "ERROR the call timed out",
    }


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
            _cited_timeout("fulfillment-api", "ledger-db"),
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
        records = [
            _cited_timeout("fulfillment-api", "ledger-db"),
            _caller_pool_record(),
        ]
        result, gateway, evidence = _investigate(
            expected["incident_type"], expected["service"], records,
        )
        _assert_owner(result, evidence, expected)
        _assert_full_return(result, gateway, evidence)

    def test_pool_limit_outranks_timeout_exception(self):
        expected = EXPECTED["pool_over_exception"]
        records = [
            _cited_timeout("invoicing-api", "invoice-store"),
            {
                "_time": "2024-11-04T07:59:20Z",
                "service": "invoicing-api",
                "downstream": "invoice-store",
                "level": "ERROR",
                "message": "SocketTimeoutException: connection pool limit reached (slots=20)",
            },
        ]
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


_OWNER_CAP_REASON = "downstream owner search cap is 3"

# One owner on each of the first four latency steps, in playbook order.
# The fifth owner is the incident field. Steps land on different workers,
# so a completion-order walk sees them backwards when those workers finish
# in reverse.
_OWNER_BY_LABEL = {
    "search_latency_logs": "db-1",
    "search_timed_out_logs": "db-2",
    "check_golden_signals": "db-3",
    "get_network_evidence": "db-4",
}
_ACTION_WORKER = {
    "search_logs": "log_worker",
    "get_change_data": "log_worker",
    "get_golden_signals": "apm_worker",
    "check_latency": "apm_worker",
    "get_network_evidence": "network_worker",
    "get_network_alerts": "network_worker",
    "query_metrics": "metrics_worker",
    "get_resource_metrics": "metrics_worker",
    "get_events": "metrics_worker",
}


def _scheduled_latency_steps(service):
    steps = []
    for step in get_evolved_playbook("latency"):
        label = step.get("label", step.get("action", ""))
        if should_skip_step("latency", str(label), service):
            continue
        steps.append(step)
    return steps


def _owner_for_step(action, params):
    query = str((params or {}).get("query") or "")
    if action == "search_logs" and query.startswith("latency OR slow"):
        return _OWNER_BY_LABEL["search_latency_logs"]
    if action == "search_logs" and query == "timed out":
        return _OWNER_BY_LABEL["search_timed_out_logs"]
    if action == "get_golden_signals":
        return _OWNER_BY_LABEL["check_golden_signals"]
    if action == "get_network_evidence":
        return _OWNER_BY_LABEL["get_network_evidence"]
    return None


class _ReverseFinishLogs:
    """Playbook workers finish in the reverse of submission order."""

    def __init__(self, service, delays):
        self.service = service
        self.delays = delays
        self.services: list[str] = []
        self.completed: list[str] = []
        self._slept: set[str] = set()
        self._lock = threading.Lock()

    def execute(self, action, params):
        params = params or {}
        called = str(params.get("service") or "")
        worker = _ACTION_WORKER.get(action, "")
        # get_network_evidence builds no service param. Key off the action.
        # Downstream searches reuse search_logs after the playbook groups
        # have already finished, so they do not change group finish order.
        if worker in self.delays:
            with self._lock:
                first = worker not in self._slept
                if first:
                    self._slept.add(worker)
            if first:
                time.sleep(self.delays[worker])
            with self._lock:
                self.completed.append(worker)
        owner = _owner_for_step(action, params)
        with self._lock:
            self.services.append(called)
        row = None
        if owner:
            row = {
                "_time": "2024-11-04T07:59:10Z",
                "service": self.service,
                "downstream": owner,
                "level": "ERROR",
                "message": f"{self.service} latency waiting on {owner}",
            }
        if action == "search_logs" or str(action).startswith("search_"):
            rows = [row] if row else []
            return {"logs": {"results": rows, "count": len(rows)}}
        if action in ("get_change_data", "get_change_records"):
            return {"changes": []}
        if action in ("get_golden_signals", "check_latency"):
            payload = {"signals": {"golden_signals": {}}}
            if row:
                payload["logs"] = {"results": [row], "count": 1}
            return payload
        if action in ("get_network_evidence", "get_network_alerts"):
            payload: dict = {"evidence": []}
            if row:
                payload["logs"] = {"results": [row], "count": 1}
            return payload
        if action in ("query_metrics", "get_resource_metrics"):
            return {"metrics": {"metrics": []}}
        if action == "get_events":
            return {"events": []}
        if row:
            return {"logs": {"results": [row], "count": 1}}
        return {}


def _group_finish_order(events, workers):
    last = {}
    for index, worker in enumerate(events):
        last[worker] = index
    return sorted(workers, key=lambda worker: last[worker])


def _investigate_latency_owners(service, downstream, delays):
    saved = {
        key: os.environ.get(key)
        for key in ("LLM_ENABLED", "PARALLEL_PLAYBOOK", "CALIBRATION_ENABLED")
    }
    os.environ["LLM_ENABLED"] = "false"
    os.environ["PARALLEL_PLAYBOOK"] = "true"
    os.environ["CALIBRATION_ENABLED"] = "false"
    try:
        sup = SentinalAISupervisor()
        assert sup._parallel_playbook is True
        gateway = _ReverseFinishLogs(service, delays)
        for name in list(sup.workers):
            sup.workers[name] = gateway
        incident = {
            "incident_id": "INC-FULL",
            "affected_service": service,
            "summary": f"{service} latency",
            "start_time": _START,
            "downstream": downstream,
        }
        sup._tls.current_incident = dict(incident)
        sup._tls.run_started = "2024-11-04T12:00:00Z"
        receipts = ReceiptCollector(case_id="INC-FULL")
        evidence = sup._execute_playbook(
            "latency", "INC-FULL", service, receipts,
            ExecutionBudget(), CircuitBreakerRegistry(),
        )
        sup._tls.last_evidence = evidence
        result = sup._analyze_evidence(
            "INC-FULL", dict(incident), "latency", evidence,
        )
        return result, gateway
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestDownstreamOwnerSearchCap:
    """At most three downstream owners are searched, in playbook order.

    Expected before the run, written against 2ec296a. Parallel mode is
    on. Workers are forced to finish in the reverse of playbook order.
    Owners still follow playbook step, then record order within the
    step, then the incident field. Five owners: three searched, two
    listed under unchecked coverage. A second run matches the first.
    """

    def test_five_owners_search_three_and_list_the_rest(self):
        service = "checkout-api"
        steps = _scheduled_latency_steps(service)
        worker_order = []
        owners = []
        for step in steps:
            worker = step["worker"]
            if worker not in worker_order:
                worker_order.append(worker)
            label = step.get("label", step.get("action", ""))
            owner = _OWNER_BY_LABEL.get(label)
            if owner:
                owners.append(owner)
        owners.append("db-5")
        delays = {
            worker: 0.3 * (len(worker_order) - index)
            for index, worker in enumerate(worker_order)
        }
        expected_finish = list(reversed(worker_order))
        expected_searched = owners[:3]
        expected_unchecked = [
            {"owner": name, "reason": _OWNER_CAP_REASON} for name in owners[3:]
        ]

        def _once():
            result, gateway = _investigate_latency_owners(service, "db-5", delays)
            searched = [name for name in gateway.services if name.startswith("db-")]
            finish = _group_finish_order(gateway.completed, worker_order)
            coverage = result["_confidence_provenance"]["unchecked_coverage"]
            return searched, coverage.get("unsearched_downstream_owners"), finish

        first = _once()
        second = _once()
        assert first == second
        assert first[2] == expected_finish
        assert first[0] == expected_searched
        assert first[1] == expected_unchecked


class _EmptyLabelLogs:
    def __init__(self):
        self.services: list[str] = []

    def execute(self, action, params):
        params = params or {}
        service = str(params.get("service") or "")
        self.services.append(service)
        if action == "search_logs" and service == "checkout-api":
            return {"logs": {"results": [{
                "_time": "2024-11-04T07:59:10Z",
                "service": "checkout-api",
                "downstream": "db-1",
                "level": "ERROR",
                "message": "checkout-api latency waiting on db-1",
            }], "count": 1}}
        if action == "search_logs":
            return {"logs": {"results": [], "count": 0}}
        return {}


class TestEmptyPlaybookLabel:
    """An empty step label uses the action as the evidence key.

    Expected before the run, written against fbd7622. The owner walk
    and the evidence dict must use that same key, so the downstream
    owner on the step's log is searched.
    """

    def test_empty_label_still_searches_the_downstream_owner(self):
        import supervisor.agent as agent_module

        steps = [{
            "worker": "log_worker",
            "action": "search_logs",
            "label": "",
            "query_hint": "latency {service}",
        }]
        saved_env = {
            key: os.environ.get(key)
            for key in ("LLM_ENABLED", "PARALLEL_PLAYBOOK", "CALIBRATION_ENABLED")
        }
        saved_playbook = agent_module.get_evolved_playbook
        os.environ["LLM_ENABLED"] = "false"
        os.environ["PARALLEL_PLAYBOOK"] = "false"
        os.environ["CALIBRATION_ENABLED"] = "false"
        agent_module.get_evolved_playbook = lambda incident_type: list(steps)
        try:
            sup = SentinalAISupervisor()
            sup._parallel_playbook = False
            gateway = _EmptyLabelLogs()
            for name in list(sup.workers):
                sup.workers[name] = gateway
            incident = {
                "incident_id": "INC-FULL",
                "affected_service": "checkout-api",
                "summary": "checkout-api latency",
                "start_time": _START,
            }
            sup._tls.current_incident = dict(incident)
            sup._tls.run_started = "2024-11-04T12:00:00Z"
            evidence = sup._execute_playbook(
                "latency", "INC-FULL", "checkout-api",
                ReceiptCollector(case_id="INC-FULL"),
                ExecutionBudget(), CircuitBreakerRegistry(),
            )
        finally:
            agent_module.get_evolved_playbook = saved_playbook
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        assert "search_logs" in evidence
        assert "" not in evidence
        assert "db-1" in gateway.services


class _AlertTextGateway:
    """Playbook logs for svc-alpha, plus an ITSM dependency list.

    Every call is kept as ``(action, service)``. A missing service is
    recorded as ``""`` so a log search with no service stays visible.
    """

    def __init__(self, dependencies, logs_for_query=None, owner_logs=None):
        self.dependencies = list(dependencies)
        self.logs_for_query = logs_for_query or {}
        self.owner_logs = owner_logs or {}
        self.calls: list[tuple[str, str]] = []

    def execute(self, action, params):
        params = params or {}
        raw = params.get("service")
        service = "" if raw is None else str(raw)
        self.calls.append((str(action), service))
        if action == "get_ci_details":
            return {"ci": {"name": service, "dependencies": list(self.dependencies)}}
        if action == "search_logs":
            if service != "svc-alpha":
                rows = [dict(row) for row in self.owner_logs.get(service, [])]
                return {"logs": {"results": rows, "count": len(rows)}}
            query = str(params.get("query") or "")
            rows = [dict(row) for row in self.logs_for_query.get(query, [])]
            return {"logs": {"results": rows, "count": len(rows)}}
        if action in ("get_known_errors", "search_incidents"):
            return {}
        if action in ("get_change_data", "get_change_records"):
            return {"changes": []}
        if action in ("get_golden_signals", "check_latency"):
            return {"signals": {"golden_signals": {}}}
        if action in ("get_network_evidence", "get_network_alerts"):
            return {"evidence": []}
        if action in ("query_metrics", "get_resource_metrics"):
            return {"metrics": {"metrics": []}}
        if action == "get_events":
            return {"events": []}
        return {}


def _latency_line(downstream=""):
    row = {
        "_time": "2024-11-04T07:59:10Z",
        "service": "svc-alpha",
        "level": "ERROR",
        "message": "svc-alpha latency elevated",
    }
    if downstream:
        row["downstream"] = downstream
    return row


def _run_alert_text(gateway, incident, parallel=False):
    saved = {
        key: os.environ.get(key)
        for key in ("LLM_ENABLED", "PARALLEL_PLAYBOOK", "CALIBRATION_ENABLED")
    }
    os.environ["LLM_ENABLED"] = "false"
    os.environ["PARALLEL_PLAYBOOK"] = "true" if parallel else "false"
    os.environ["CALIBRATION_ENABLED"] = "false"
    try:
        sup = SentinalAISupervisor()
        sup._parallel_playbook = parallel
        for name in list(sup.workers):
            sup.workers[name] = gateway
        sup._tls.current_incident = dict(incident)
        sup._tls.run_started = "2024-11-04T12:00:00Z"
        receipts = ReceiptCollector(case_id="INC-ALERT")
        budget = ExecutionBudget()
        circuits = CircuitBreakerRegistry()
        sup._fetch_itsm_context(
            "svc-alpha", incident.get("summary") or "", receipts, budget, circuits,
        )
        evidence = sup._execute_playbook(
            "latency", "INC-ALERT", "svc-alpha", receipts, budget, circuits,
        )
        sup._tls.last_evidence = evidence
        result = sup._analyze_evidence(
            "INC-ALERT", dict(incident), "latency", evidence,
        )
        return result, gateway, receipts
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _alert_incident(**extra):
    incident = {
        "incident_id": "INC-ALERT",
        "affected_service": "svc-alpha",
        "summary": "svc-alpha latency",
        "start_time": _START,
    }
    incident.update(extra)
    return incident


def _search_log_services(gateway) -> list[str]:
    """Services on search_logs calls, including an empty service."""
    return [service for action, service in gateway.calls if action == "search_logs"]


def _alert_snapshot(result, gateway, receipts):
    cause = result["cause"]
    coverage = result["_confidence_provenance"]["unchecked_coverage"]
    marks = [
        (receipt.params.get("service"), receipt.params.get("owner_source"))
        for receipt in receipts.receipts
        if receipt.action == "search_logs" and receipt.params.get("owner_source")
    ]
    return {
        "statement": cause["statement"],
        "confidence": cause["confidence"],
        "category": cause["category"],
        "refs": cause["evidence_refs"],
        "unchecked": coverage.get("unsearched_downstream_owners"),
        "log_searches": _search_log_services(gateway),
        "marks": marks,
    }


class TestAlertTextDownstreamRetrieval:
    """A downstream named only in the alert is retrieved, not cited.

    Written against e3923cc. The name must exactly match a service in
    this incident's ITSM topology receipt. The learned topology does
    not count. Alert-text owners follow record and structured owners
    and share the cap of 3.
    """

    def test_known_topology_name_in_the_alert_is_searched(self):
        gateway = _AlertTextGateway(
            ["ledger-store"],
            logs_for_query={"latency OR slow svc-alpha": [_latency_line()]},
        )
        result, gateway, receipts = _run_alert_text(
            gateway,
            _alert_incident(
                title="latency while calling ledger-store",
                description="svc-alpha is slow",
            ),
        )
        assert _search_log_services(gateway) == [
            "svc-alpha", "svc-alpha", "ledger-store",
        ]
        assert ("ledger-store", "alert_text") in _alert_snapshot(result, gateway, receipts)["marks"]

    def test_unknown_alert_word_starts_no_search(self):
        gateway = _AlertTextGateway(
            ["ledger-store"],
            logs_for_query={"latency OR slow svc-alpha": [_latency_line()]},
        )
        _result, gateway, receipts = _run_alert_text(
            gateway,
            _alert_incident(title="latency while calling widget-blob"),
        )
        assert _search_log_services(gateway) == ["svc-alpha", "svc-alpha"]
        assert _alert_snapshot(_result, gateway, receipts)["marks"] == []

    def test_empty_alert_text_search_stays_unknown(self):
        gateway = _AlertTextGateway(
            ["ledger-store"],
            logs_for_query={"latency OR slow svc-alpha": [_latency_line()]},
            owner_logs={"ledger-store": []},
        )
        result, gateway, _receipts = _run_alert_text(
            gateway,
            _alert_incident(description="callers are waiting on ledger-store"),
        )
        assert _search_log_services(gateway) == [
            "svc-alpha", "svc-alpha", "ledger-store",
        ]
        cause = result["cause"]
        assert "UNKNOWN" in cause["statement"]
        assert cause["category"] == "unknown"
        assert "ledger-store" not in cause["statement"]
        assert all(ref.get("service") != "ledger-store" for ref in cause["evidence_refs"])
        assert all(
            "ledger-store" not in str(ref.get("signal") or "")
            for ref in cause["evidence_refs"]
        )

    def test_learned_topology_alone_starts_nothing(self):
        import tempfile

        import intelligence.topology_learner as topo
        from intelligence.causal_graph import CausalGraph

        incident = _alert_incident(title="latency while calling ledger-store")
        saved = (topo._singleton_graph, topo._singleton_learner)

        def _once():
            gateway = _AlertTextGateway(
                [],
                logs_for_query={"latency OR slow svc-alpha": [_latency_line()]},
            )
            result, gateway, receipts = _run_alert_text(gateway, incident)
            return _alert_snapshot(result, gateway, receipts)

        try:
            topo._singleton_graph = None
            topo._singleton_learner = None
            fresh = _once()
            with tempfile.TemporaryDirectory() as directory:
                graph = CausalGraph(storage_path=f"{directory}/topo.jsonl")
                graph.record_co_failure("svc-alpha", "ledger-store", 12)
                topo._singleton_graph = graph
                topo._singleton_learner = topo.TopologyLearner(graph)
                seeded = _once()
        finally:
            topo._singleton_graph, topo._singleton_learner = saved
        assert fresh == seeded
        assert fresh["log_searches"] == ["svc-alpha", "svc-alpha"]
        assert fresh["marks"] == []

    def test_alert_text_owner_is_after_the_cap(self):
        gateway = _AlertTextGateway(
            ["index-eta"],
            logs_for_query={
                "latency OR slow svc-alpha": [_latency_line("ledger-store")],
                "timed out": [_latency_line("cache-zeta")],
            },
        )
        result, gateway, _receipts = _run_alert_text(
            gateway,
            _alert_incident(
                title="also see index-eta",
                downstream_service="queue-beta",
            ),
        )
        assert _search_log_services(gateway) == [
            "svc-alpha",
            "svc-alpha",
            "ledger-store",
            "cache-zeta",
            "queue-beta",
        ]
        coverage = result["_confidence_provenance"]["unchecked_coverage"]
        assert {"owner": "index-eta", "reason": _OWNER_CAP_REASON} in (
            coverage.get("unsearched_downstream_owners") or []
        )

    def test_parallel_runs_match(self):
        incident = _alert_incident(title="latency while calling ledger-store")

        def _once():
            gateway = _AlertTextGateway(
                ["ledger-store"],
                logs_for_query={"latency OR slow svc-alpha": [_latency_line()]},
            )
            result, gateway, receipts = _run_alert_text(gateway, incident, parallel=True)
            return _alert_snapshot(result, gateway, receipts)

        assert _once() == _once()


_MEMORY_NAME = "ledger-sync-zz"


def _memory_row():
    return {"service": _MEMORY_NAME, "summary": "earlier latency on another run"}


def _completed(value):
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


def _memory_sources(place):
    """Put the memory name in exactly one post-playbook source, or in all."""
    row = _memory_row()
    itsm = None
    historical = None
    kg = []
    if place in ("similar_incidents", "all"):
        itsm = {"similar_incidents": [dict(row)]}
    if place in ("known_errors", "all"):
        itsm = dict(itsm or {})
        itsm["known_errors"] = [dict(row)]
    if place in ("historical_context", "all"):
        historical = {"similar_incidents": [dict(row)]}
    if place in ("kg_similar", "all"):
        kg = [dict(row)]
    if itsm is not None:
        itsm["ci"] = {"name": "svc-alpha", "dependencies": []}
    return itsm, historical, kg


def _nested_strings(value):
    found = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_nested_strings(key))
            found.extend(_nested_strings(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_nested_strings(item))
    return found


def _retrieved_log_services(evidence):
    names = []

    def walk(value):
        if isinstance(value, dict):
            logs = value.get("logs")
            if isinstance(logs, dict):
                for row in logs.get("results") or []:
                    if isinstance(row, dict):
                        if row.get("service"):
                            names.append(row["service"])
                        if row.get("downstream"):
                            names.append(row["downstream"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(evidence)
    return names


def _assert_no_memory_search(gateway, receipts, evidence):
    assert _search_log_services(gateway) == ["svc-alpha", "svc-alpha"]
    assert all(
        (receipt.params or {}).get("service") != _MEMORY_NAME
        for receipt in receipts.receipts
        if receipt.action == "search_logs"
    )
    assert all(
        (receipt.params or {}).get("owner_source") != "alert_text"
        for receipt in receipts.receipts
    )
    for receipt in receipts.receipts:
        recorded = getattr(receipt, "topology_services", None) or []
        assert _MEMORY_NAME not in recorded
    assert _MEMORY_NAME not in _retrieved_log_services(evidence)


def _run_memory_collect(place):
    """Run collect so memory merges happen after the owner search."""
    import supervisor.trace_correlation as traces
    import workers.visual_evidence_worker as visual
    from sentinel_core.context import ContextBuilder
    from supervisor.phases.classify import ClassificationResult
    from supervisor.phases.collect import CollectPhase

    itsm, historical, kg = _memory_sources(place)
    incident = _alert_incident(title=f"latency while calling {_MEMORY_NAME}")
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "LLM_ENABLED",
            "PARALLEL_PLAYBOOK",
            "CALIBRATION_ENABLED",
            "AGENTIC_PLANNER",
            "LOOP_CONTROLLER_ENABLED",
        )
    }
    saved_fns = (traces.correlate_traces, visual.collect_visual_evidence)
    os.environ["LLM_ENABLED"] = "false"
    os.environ["PARALLEL_PLAYBOOK"] = "false"
    os.environ["CALIBRATION_ENABLED"] = "false"
    os.environ["AGENTIC_PLANNER"] = "false"
    os.environ["LOOP_CONTROLLER_ENABLED"] = "false"
    traces.correlate_traces = lambda *_args, **_kwargs: None
    visual.collect_visual_evidence = lambda *_args, **_kwargs: {}
    try:
        sup = SentinalAISupervisor()
        sup._parallel_playbook = False
        gateway = _AlertTextGateway(
            [],
            logs_for_query={
                "latency OR slow svc-alpha": [_latency_line()],
                "timed out": [_latency_line()],
            },
        )
        for name in list(sup.workers):
            sup.workers[name] = gateway
        sup._tls.current_incident = dict(incident)
        sup._tls.run_started = "2024-11-04T12:00:00Z"
        receipts = ReceiptCollector(case_id="INC-ALERT")
        budget = ExecutionBudget()
        circuits = CircuitBreakerRegistry()
        classification = ClassificationResult(
            incident_type="latency",
            severity=None,
            budget=budget,
            itsm_context=itsm,
            confluence_context=None,
            experience_future=_completed([]),
            kg_future=_completed(kg),
            historical_future=_completed(historical),
        )
        result = CollectPhase(sup).execute(
            ContextBuilder.for_incident("INC-ALERT", incident=dict(incident)),
            {
                "incident": dict(incident),
                "summary": incident["summary"],
                "service": "svc-alpha",
                "receipts": receipts,
                "circuits": circuits,
            },
            classification,
        )
        evidence = result.output.result["collect"].evidence
        return evidence, gateway, receipts
    finally:
        traces.correlate_traces, visual.collect_visual_evidence = saved_fns
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestMemoryNamesAreNotKnownServices:
    """Names carried in from memory are not known services for this run.

    The owner search walks whatever is already in the evidence dict.
    Collect merges ITSM similar incidents, known errors, historical
    context, and KG similar incidents only after that search. These
    guards fail if that merge moves ahead of the playbook.
    """

    @pytest.mark.parametrize("place", [
        "similar_incidents",
        "known_errors",
        "historical_context",
        "kg_similar",
    ])
    def test_memory_name_in_the_alert_starts_no_search(self, place):
        evidence, gateway, receipts = _run_memory_collect(place)
        _assert_no_memory_search(gateway, receipts, evidence)
        if place == "similar_incidents":
            assert evidence["itsm_context"]["similar_incidents"] == [_memory_row()]
            assert "known_errors" not in evidence["itsm_context"]
            assert "historical_context" not in evidence
            assert "_kg_similar_incidents" not in evidence
        elif place == "known_errors":
            assert evidence["itsm_context"]["known_errors"] == [_memory_row()]
            assert "similar_incidents" not in evidence["itsm_context"]
            assert "historical_context" not in evidence
            assert "_kg_similar_incidents" not in evidence
        elif place == "historical_context":
            assert evidence["historical_context"]["similar_incidents"] == [_memory_row()]
            assert "itsm_context" not in evidence
            assert "_kg_similar_incidents" not in evidence
        else:
            assert evidence["_kg_similar_incidents"] == [_memory_row()]
            assert "itsm_context" not in evidence
            assert "historical_context" not in evidence

    def test_merged_evidence_is_absent_during_the_owner_search(self):
        captured = {}
        real = SentinalAISupervisor._search_downstream_owners

        def _spy(self, evidence, *args, **kwargs):
            captured["evidence"] = copy.deepcopy(evidence)
            return real(self, evidence, *args, **kwargs)

        SentinalAISupervisor._search_downstream_owners = _spy
        try:
            evidence, gateway, receipts = _run_memory_collect("all")
        finally:
            SentinalAISupervisor._search_downstream_owners = real

        during = captured["evidence"]
        memory_keys = {"itsm_context", "historical_context", "_kg_similar_incidents"}
        assert memory_keys.isdisjoint(during)
        assert _MEMORY_NAME not in _nested_strings(during)
        assert memory_keys <= set(evidence)
        assert set(during) <= set(evidence)
        assert evidence["itsm_context"]["similar_incidents"] == [_memory_row()]
        assert evidence["itsm_context"]["known_errors"] == [_memory_row()]
        assert evidence["historical_context"]["similar_incidents"] == [_memory_row()]
        assert evidence["_kg_similar_incidents"] == [_memory_row()]
        assert _MEMORY_NAME in _nested_strings(evidence)
        _assert_no_memory_search(gateway, receipts, evidence)
