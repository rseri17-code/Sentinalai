"""Acceptance criteria v1.13. Made-up services only."""

from __future__ import annotations

import ast
from pathlib import Path

from supervisor.agent import Hypothesis, SentinalAISupervisor
from supervisor.helpers.cause_binding import (
    bind_hypothesis,
    query_ref_ok,
    require_query_tie,
    unchecked_coverage,
)
from supervisor.helpers.metric_series import (
    GATEWAY_EMITTERS,
    GOLDEN_FORMAT,
    SERIES_FORMAT,
    classify_metric_payload,
    normalize_metric_payload,
)
from supervisor.helpers.timeout_evidence import decide_timeout
from supervisor.receipt import ReceiptCollector

_WHEN = "2024-08-01T12:00:00Z"
_LOG_TS = "2024-08-01T12:00:10Z"
_CHANGE_TS = "2024-08-01T11:55:00Z"


def _incident(**extra) -> dict:
    incident = {
        "affected_service": "svc-alpha",
        "start_time": _WHEN,
        "title": "svc-alpha error",
        "description": "",
    }
    incident.update(extra)
    return incident


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        name="probe",
        root_cause="error",
        base_score=10,
        evidence_refs=[],
        reasoning="",
    )


def _bind(evidence, incident=None):
    return bind_hypothesis(
        _hypothesis(),
        incident_type="error_spike",
        service="svc-alpha",
        incident=incident or _incident(),
        evidence=evidence,
    )


def _exception_log(service="svc-alpha", message="IllegalStateException while writing"):
    return {
        "_time": _LOG_TS,
        "service": service,
        "level": "ERROR",
        "message": message,
    }


def _evidence(logs, changes=None, **extra_payloads):
    evidence = {
        "search_logs": {
            "_receipt_sequence_order": 2,
            "_receipt_tool": "log_worker",
            "logs": {"results": logs, "count": len(logs)},
        },
    }
    if changes is not None:
        evidence["get_changes"] = {
            "_receipt_sequence_order": 3,
            "_receipt_tool": "change_worker",
            "changes": changes,
        }
    evidence.update(extra_payloads)
    return evidence


def _cause_services(assessment) -> list[str]:
    return [str(ref.get("service") or "") for ref in assessment["cause_refs"]]


class TestChangeBelongsToItsService:
    def test_unrelated_deploy_is_not_cited_or_named(self):
        alone = _bind(_evidence([_exception_log()]))
        with_other = _bind(_evidence(
            [_exception_log()],
            [{
                "id": "CHG-9",
                "change_type": "deployment",
                "service": "cache-zeta",
                "description": "deploy cache-zeta 9.9.9",
                "scheduled_start": _CHANGE_TS,
            }],
        ))
        assert with_other["cause_confidence"] == alone["cause_confidence"] == 62
        assert "cache-zeta" not in with_other["statement"]
        assert "9.9.9" not in with_other["statement"]
        assert "cache-zeta" not in _cause_services(with_other)
        other = with_other["other_changes"]
        assert other["statement"] == "other changes in window"
        assert other["evidence_refs"][0]["service"] == "cache-zeta"
        assert other["evidence_refs"][0]["signal"] == "other_change"

    def test_owner_deploy_is_only_the_preceding_change(self):
        assessment = _bind(_evidence(
            [_exception_log()],
            [{
                "id": "CHG-1",
                "change_type": "deployment",
                "service": "  SVC-Alpha ",
                "description": "release 4.8.2 of svc-alpha",
                "scheduled_start": _CHANGE_TS,
            }],
        ))
        assert assessment["statement"] == (
            "IllegalStateException in svc-alpha 4.8.2, deployed at 2024-08-01T11:55:00Z"
        )
        assert assessment["cause_confidence"] == 70
        assert any(ref.get("signal") == "deployment" for ref in assessment["cause_refs"])
        assert "whether the change caused it" in assessment["unknowns"]
        assert "whether the deploy introduced the error" in assessment["unknowns"]

    def test_change_without_a_service_goes_to_unknowns(self):
        assessment = _bind(_evidence(
            [_exception_log()],
            [{
                "id": "CHG-0",
                "change_type": "deployment",
                "description": "release 8.8.8",
                "scheduled_start": _CHANGE_TS,
            }],
        ))
        assert "change in window, service not identified" in assessment["unknowns"]
        assert "8.8.8" not in assessment["statement"]
        assert not any(ref.get("signal") == "deployment" for ref in assessment["cause_refs"])
        assert "other_changes" not in assessment

    def test_prefix_near_match_does_not_match(self):
        assessment = _bind(
            _evidence(
                [_exception_log("svc-a")],
                [{
                    "change_type": "deployment",
                    "service": "svc-a-canary",
                    "description": "deploy svc-a-canary 1.2.3",
                    "scheduled_start": _CHANGE_TS,
                }],
            ),
            _incident(affected_service="svc-a"),
        )
        assert "svc-a-canary" not in assessment["statement"]
        assert "1.2.3" not in assessment["statement"]
        assert assessment["cause_confidence"] == 62
        assert assessment["other_changes"]["evidence_refs"][0]["service"] == "svc-a-canary"

    def test_dependency_change_stays_another_services_change(self):
        assessment = _bind(_evidence(
            [_exception_log()],
            [{
                "change_type": "deployment",
                "ci": "ledger-store",
                "description": "deploy ledger-store",
                "scheduled_start": _CHANGE_TS,
            }],
        ))
        assert "ledger-store" not in assessment["statement"]
        assert "ledger-store" not in _cause_services(assessment)
        assert assessment["other_changes"]["evidence_refs"][0]["service"] == "ledger-store"

    def test_ci_field_matches_the_owner(self):
        assessment = _bind(_evidence(
            [_exception_log()],
            [{
                "change_type": "deployment",
                "ci": "svc-alpha",
                "description": "release 4.8.2",
                "scheduled_start": _CHANGE_TS,
            }],
        ))
        assert any(ref.get("signal") == "deployment" for ref in assessment["cause_refs"])
        assert "whether the change caused it" in assessment["unknowns"]


def _timeout(records, incident=None):
    incident = incident or _incident()
    evidence = {
        "search_logs": {
            "_receipt_sequence_order": 4,
            "_receipt_tool": "log_worker",
            "logs": {"results": records, "count": len(records)},
        },
    }
    return decide_timeout(
        service=incident["affected_service"],
        logs=records,
        signals={},
        metrics={},
        incident=incident,
        evidence=evidence,
    )


def _pool(service, downstream):
    return {
        "_time": "2024-08-01T12:00:05Z",
        "service": service,
        "level": "ERROR",
        "message": "ERROR connection pool exhausted",
        "downstream": downstream,
    }


class TestPoolMatchesFailingDependency:
    def test_pool_for_a_with_timeout_to_b_is_not_a_cause(self):
        decision = _timeout([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR timeout talking to upstream",
                "target": "cache-zeta",
            },
            _pool("svc-alpha", "ledger-store"),
        ])
        assert decision["category"] != "connection_pool_exhaustion"
        assert decision["cause_confidence"] < 60
        assert not any(
            ref.get("signal") == "connection_pool_exhausted"
            for ref in decision["cause_refs"]
        )
        observed = decision["observations"]
        assert observed[0]["statement"] == "pool to ledger-store exhausted"
        assert observed[0]["evidence_refs"][0]["service"] == "svc-alpha"
        assert "failing dependency not identified" not in decision["unknowns"]

    def test_pool_for_b_with_timeout_to_b_binds(self):
        decision = _timeout([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR timeout talking to upstream",
                "downstream": "cache-zeta",
            },
            _pool("svc-alpha", "cache-zeta"),
        ])
        assert decision["category"] == "connection_pool_exhaustion"
        assert decision["statement"] == "svc-alpha's connection pool to cache-zeta exhausted"
        assert decision["cause_confidence"] == 62
        assert decision["cause_refs"][0]["signal"] == "connection_pool_exhausted"

    def test_timeout_without_a_target_plus_pool_for_a_is_unknown(self):
        decision = _timeout([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR the call timed out",
            },
            _pool("svc-alpha", "ledger-store"),
        ])
        assert decision["category"] == "unknown"
        assert decision["cause_confidence"] < 60
        assert "failing dependency not identified" in decision["unknowns"]
        assert not decision["cause_refs"]

    def test_dependency_named_only_in_alert_text_is_unknown(self):
        incident = _incident(
            title="ledger-store is down",
            description="callers of ledger-store are timing out",
            summary="ledger-store pool",
        )
        decision = _timeout([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR the call timed out",
            },
            _pool("svc-alpha", "ledger-store"),
        ], incident)
        assert decision["category"] == "unknown"
        assert decision["cause_confidence"] < 60
        assert "failing dependency not identified" in decision["unknowns"]
        assert not any(
            ref.get("signal") == "connection_pool_exhausted"
            for ref in decision["cause_refs"]
        )

    def test_structured_timeout_ignores_a_pool_with_no_downstream(self):
        decision = _timeout([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR timeout talking to upstream",
                "target": "cache-zeta",
            },
            {
                "_time": "2024-08-01T12:00:05Z",
                "service": "cache-zeta",
                "level": "ERROR",
                "message": "ERROR connection pool exhausted",
            },
        ])
        assert decision["category"] != "connection_pool_exhaustion"
        assert decision["cause_confidence"] < 60

    def test_span_from_the_alerted_service_establishes_the_dependency(self):
        decision = _timeout([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR the call timed out",
            },
            {
                "_time": "2024-08-01T12:00:02Z",
                "service": "svc-alpha",
                "from": "svc-alpha",
                "to": "cache-zeta",
                "span_id": "span-1",
                "message": "client span",
            },
            _pool("svc-alpha", "cache-zeta"),
        ])
        assert decision["category"] == "connection_pool_exhaustion"
        assert decision["cause_confidence"] >= 60


class TestMetricShapes:
    def test_shape_metrics_contract(self):
        payload = {
            "metrics": {
                "metrics": [
                    {
                        "timestamp": "2024-08-01T12:00:00Z",
                        "value": 2,
                        "metric": "db_pool_active",
                        "service": "svc-alpha",
                    },
                    {
                        "timestamp": "2024-08-01T12:01:00Z",
                        "value": 4,
                        "metric": "latency_p95_ms",
                        "service": "svc-alpha",
                    },
                ],
                "baseline": 2,
                "pattern": "none",
            },
            "source": "prometheus",
        }
        assert classify_metric_payload(payload) == "series"
        by_metric = {row["metric"]: row for row in normalize_metric_payload(payload)}
        pool = by_metric["db_pool_active"]
        latency = by_metric["latency_p95_ms"]
        assert pool["source_format"] == SERIES_FORMAT
        assert pool["service"] == "svc-alpha"
        assert pool["unit"] == "1"
        assert pool["points"] == [("2024-08-01T12:00:00Z", 2.0)]
        assert latency["unit"] == "ms"
        assert latency["points"] == [("2024-08-01T12:01:00Z", 4.0)]

    def test_shape_golden_signals_contract(self):
        payload = {
            "signals": {
                "service": "cache-zeta",
                "timestamp": "2024-08-01T12:00:00Z",
                "golden_signals": {
                    "latency": {"p95": 80.0, "baseline_p95": 20.0},
                    "errors": {"rate": 0.01},
                    "traffic": {"rps": 12.0},
                    "saturation": {"pct": 40.0},
                },
            },
            "metrics": {"service": "cache-zeta", "p95_ms": 80.0},
            "source": "prometheus",
        }
        assert classify_metric_payload(payload) == "golden"
        by_metric = {row["metric"]: row for row in normalize_metric_payload(payload)}
        assert by_metric["latency_p95"]["unit"] == "ms"
        assert by_metric["latency_p95"]["points"] == [("2024-08-01T12:00:00Z", 80.0)]
        assert by_metric["latency_p95"]["source_format"] == GOLDEN_FORMAT
        assert by_metric["latency_p95"]["service"] == "cache-zeta"
        assert by_metric["error_rate"]["unit"] == "ratio"
        assert by_metric["request_rate"]["unit"] == "rps"
        assert by_metric["request_rate"]["points"] == [("2024-08-01T12:00:00Z", 12.0)]
        assert by_metric["saturation_pct"]["unit"] == "percent"

    def test_gateway_metric_shapes_have_normalizers_and_tests(self):
        source = Path("oss_validation_gateway/shaping.py").read_text()
        tree = ast.parse(source)
        emitters = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("shape_"):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if "golden_signals" in segment or (
                '"metrics"' in segment and "baseline" in segment and "pattern" in segment
            ):
                emitters.append(node.name)
        assert set(emitters) == set(GATEWAY_EMITTERS)
        this = Path(__file__).read_text()
        for emitter, fmt in GATEWAY_EMITTERS.items():
            assert fmt in {SERIES_FORMAT, GOLDEN_FORMAT}
            assert f"def test_{emitter}_contract" in this

    def test_unparsed_body_is_not_empty_or_zero(self):
        weird = {
            "data": {"resultType": "matrix", "result": [{"value": [0, "0"]}]},
            "metrics": [{"name": "db_pool_active", "value": 0, "timestamp": _LOG_TS}],
        }
        assert classify_metric_payload(weird) == "unparsed"
        logs = [_exception_log()]
        plain = _bind(_evidence(logs))
        marked = _bind(_evidence(logs, vendor_body={
            "_receipt_sequence_order": 8,
            "_receipt_tool": "metrics_worker",
            **weird,
        }))
        assert marked["cause_confidence"] == plain["cause_confidence"]
        assert marked["statement"] == plain["statement"]
        assert "matrix" not in marked["statement"]
        coverage = unchecked_coverage(_incident(), {
            "vendor_body": {"_receipt_tool": "metrics_worker", **weird},
        })
        assert coverage["unavailable_signals"] == [
            {"evidence_key": "vendor_body", "reason": "unparsed_format"},
        ]

    def test_contradicting_series_keeps_the_cause_below_60(self):
        evidence = _evidence([
            {
                "_time": _LOG_TS,
                "service": "svc-alpha",
                "level": "ERROR",
                "message": "ERROR connection pool exhausted",
            },
        ])
        evidence["query_metrics"] = {
            "_receipt_sequence_order": 6,
            "_receipt_tool": "metrics_worker",
            "metrics": {
                "metrics": [
                    {
                        "timestamp": "2024-08-01T12:00:00Z",
                        "value": 30,
                        "metric": "db_pool_active",
                        "service": "svc-alpha",
                    },
                    {
                        "timestamp": "2024-08-01T12:00:20Z",
                        "value": 3,
                        "metric": "db_pool_active",
                        "service": "svc-alpha",
                    },
                ],
                "baseline": 30,
                "pattern": "none",
            },
        }
        assessment = _bind(evidence)
        assert assessment["category"] == "unknown"
        assert assessment["cause_confidence"] < 60
        refs = [
            ref
            for group in assessment["contradictions"]
            for ref in group["evidence_refs"]
        ]
        assert {ref.get("signal") for ref in refs} >= {
            "connection_pool_exhausted",
            "metric_series",
        }

    def test_aligned_pool_series_for_the_owner_binds(self):
        evidence = {
            "query_metrics": {
                "_receipt_sequence_order": 6,
                "_receipt_tool": "metrics_worker",
                "metrics": {
                    "metrics": [
                        {
                            "timestamp": _LOG_TS,
                            "value": 30,
                            "metric": "db_pool_active",
                            "service": "svc-alpha",
                        },
                    ],
                    "baseline": 30,
                    "pattern": "none",
                },
            },
        }
        assessment = _bind(evidence)
        assert assessment["category"] == "connection_pool_exhaustion"
        assert assessment["cause_confidence"] >= 60
        assert "svc-alpha" in assessment["statement"]
        assert assessment["cause_refs"][0]["service"] == "svc-alpha"

    def test_golden_summary_does_not_bind_a_cause(self):
        evidence = {
            "check_signals": {
                "_receipt_sequence_order": 1,
                "_receipt_tool": "apm_worker",
                "signals": {
                    "service": "svc-alpha",
                    "timestamp": _LOG_TS,
                    "golden_signals": {"latency": {"p95": 900.0, "baseline_p95": 20.0}},
                },
            },
        }
        assessment = _bind(evidence)
        assert assessment["category"] == "unknown"
        assert assessment["cause_confidence"] < 60


class TestQueryCoverage:
    def _window(self, query_id, source, truncated, logs):
        return {
            "_receipt_sequence_order": 2 if query_id == "q2" else 1,
            "_receipt_tool": "log_worker",
            "_query_id": query_id,
            "_filter": "svc-alpha" if source == "playbook_hint" else "cache-zeta",
            "_filter_source": source,
            "_window_start": "2024-08-01T11:45:00Z",
            "_window_end": "2024-08-01T12:15:00Z",
            "_oldest_ts": "2024-08-01T12:00:00Z",
            "_newest_ts": "2024-08-01T12:00:10Z",
            "_limit": 1,
            "_truncated": truncated,
            "logs": {"results": logs, "count": len(logs)},
        }

    def test_broad_gap_does_not_invalidate_a_targeted_hit(self):
        evidence = {
            "search_logs": self._window("q1", "playbook_hint", True, [{
                "_time": "2024-08-01T12:00:00Z",
                "service": "svc-alpha",
                "level": "INFO",
                "message": "healthcheck ok",
            }]),
            "search_downstream_cache_zeta_logs": self._window(
                "q2", "cited_record_field", False, [_exception_log()],
            ),
        }
        assessment = _bind(evidence)
        assert assessment["category"] == "exception"
        assert assessment["cause_confidence"] >= 60
        ref = assessment["cause_refs"][0]
        assert ref["query_id"] == "q2"
        assert query_ref_ok(ref, evidence)
        gaps = unchecked_coverage(_incident(), evidence)["query_gaps"]
        assert [row["query_id"] for row in gaps] == ["q1"]
        assert gaps[0]["truncated"] is True

    def test_capped_targeted_query_lists_its_gap(self):
        evidence = {
            "search_downstream_cache_zeta_logs": self._window(
                "q2", "cited_record_field", True, [_exception_log()],
            ),
        }
        assessment = _bind(evidence)
        assert assessment["cause_confidence"] >= 60
        assert query_ref_ok(assessment["cause_refs"][0], evidence)
        gaps = unchecked_coverage(_incident(), evidence)["query_gaps"]
        assert gaps == [{
            "evidence_key": "search_downstream_cache_zeta_logs",
            "query_id": "q2",
            "window_start": "2024-08-01T11:45:00Z",
            "window_end": "2024-08-01T12:15:00Z",
            "oldest_ts": "2024-08-01T12:00:00Z",
            "newest_ts": "2024-08-01T12:00:10Z",
            "limit": 1,
            "truncated": True,
            "filter_source": "cited_record_field",
        }]

    def test_ref_without_a_query_id_fails(self):
        evidence = _evidence([_exception_log()])
        assessment = _bind(evidence)
        ref = assessment["cause_refs"][0]
        assert "query_id" not in ref
        assert query_ref_ok(ref, evidence) is False
        assert query_ref_ok({**ref, "query_id": "q-missing"}, evidence) is False

    def test_worker_call_records_the_query_outside_params(self):
        class _Worker:
            def execute(self, action, params):
                return {
                    "logs": {"results": [], "count": 0},
                    "limit": 2,
                    "truncated": True,
                    "oldest_ts": "2024-08-01T12:00:00Z",
                    "newest_ts": "2024-08-01T12:00:10Z",
                    "window_start": "2024-08-01T11:45:00Z",
                    "window_end": "2024-08-01T12:15:00Z",
                }

        receipts = ReceiptCollector(case_id="case-1")
        result = SentinalAISupervisor()._call_worker(
            _Worker(),
            "search_logs",
            {"query": "cache-zeta", "service": "cache-zeta"},
            receipts,
            None,
            "log_worker",
            filter_source="cited_record_field",
        )
        receipt = receipts.receipts[0]
        assert result["_query_id"] == receipt.query_id == "q1"
        assert receipt.filter == "cache-zeta"
        assert receipt.filter_source == "cited_record_field"
        assert receipt.window_start == "2024-08-01T11:45:00Z"
        assert receipt.window_end == "2024-08-01T12:15:00Z"
        assert receipt.oldest_ts == "2024-08-01T12:00:00Z"
        assert receipt.newest_ts == "2024-08-01T12:00:10Z"
        assert receipt.limit == 2
        assert receipt.truncated is True
        assert "query_id" not in receipt.params
        stored = receipt.to_dict()
        assert stored["query_id"] == "q1"
        assert stored["truncated"] is True


def _cited_refs(result: dict) -> list[dict]:
    """Every ref in the cause output, including contradictions."""
    cause = result.get("cause") or {}
    found = []
    for ref in cause.get("evidence_refs") or []:
        if isinstance(ref, dict):
            found.append(ref)
    for group in cause.get("contradictions") or []:
        if not isinstance(group, dict):
            continue
        for ref in group.get("evidence_refs") or []:
            if isinstance(ref, dict):
                found.append(ref)
    symptom = result.get("symptom") or {}
    for ref in symptom.get("evidence_refs") or []:
        if isinstance(ref, dict):
            found.append(ref)
    return found


def test_query_tie_rescore_does_not_raise_confidence():
    """Dropping an untied ref must not raise the published confidence.

    One remaining raw ref would rescore to 62. The decision was already
    at 34, so it stays 34, the loose record is not cited, and the result
    is UNKNOWN because what remains is below 60.
    """
    evidence = {
        "search_logs": {
            "_query_id": "q1",
            "_filter_source": "playbook_hint",
            "_receipt_tool": "log_worker",
            "logs": {"results": [], "count": 0},
        },
    }
    tied = {
        "query_id": "q1",
        "signal": "IllegalStateException",
        "service": "svc-alpha",
        "timestamp": "",
        "evidence_class": "raw",
    }
    loose = {
        "query_id": "q-missing",
        "signal": "deployment",
        "service": "cache-zeta",
        "timestamp": "2024-08-01T11:55:00Z",
        "evidence_class": "raw",
    }
    decision = {
        "statement": "IllegalStateException in svc-alpha",
        "category": "exception",
        "cause_confidence": 34,
        "cause_refs": [tied, loose],
        "contradictions": [],
        "unknowns": [],
        "contributions": [],
    }
    out = require_query_tie(decision, evidence)
    assert out["cause_confidence"] <= 34
    assert all(ref.get("service") != "cache-zeta" for ref in out["cause_refs"])
    assert "cache-zeta" not in " ".join(
        str(ref.get("signal") or "") for ref in out["cause_refs"]
    )
    named = " ".join(out["unknowns"])
    assert "UNKNOWN" in out["statement"] or "cache-zeta" in named
    assert out["cause_confidence"] < 60


def test_every_cited_cause_ref_resolves_to_its_query():
    """C4. Each cited ref on the fixture incidents resolves to a receipt.

    The id is the one the engine recorded for that query. The record's
    timestamp sits inside that receipt's window and returned span.
    """
    from tests.fixtures.mock_mcp_responses import ALL_MOCKS
    from tests.test_supervisor import _build_mock_workers

    misses = []
    for incident_id in ALL_MOCKS:
        supervisor = SentinalAISupervisor()
        _build_mock_workers(supervisor, incident_id)
        result = supervisor.investigate(incident_id)
        receipts = result.get("receipts") or []
        for ref in _cited_refs(result):
            if query_ref_ok(ref, receipts=receipts):
                continue
            misses.append(
                f"{incident_id} signal={ref.get('signal')!r} "
                f"query_id={ref.get('query_id')!r} ts={ref.get('timestamp')!r}"
            )
    assert misses == []
