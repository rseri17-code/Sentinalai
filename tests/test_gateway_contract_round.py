"""Contract proofs for this round. Every assertion reads investigate().

C2 and C3 payloads are produced by importing the validation gateway and
calling it. They are not hand-copied shapes. The regression counts are
the six supported causes in tests/test_full_log_binding.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from oss_validation_gateway.backends import Backends, Settings
from oss_validation_gateway.dispatch import dispatch
from oss_validation_gateway.protocol import handle_rpc
from oss_validation_gateway.shaping import shape_golden_signals, shape_logs, shape_metrics
from supervisor.agent import SentinalAISupervisor
from supervisor.helpers.timeout_evidence import resolve_evidence_ref
from tests.test_full_log_binding import EXPECTED

_START = "2024-11-04T08:00:00Z"
_PHRASE = "what caused the incident wasn't established"
_MODES = ("all_logs", "frozen", "loki")


def _unix(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _ns(iso: str) -> str:
    return str(int(_unix(iso)) * 1_000_000_000)


def _tools_call(payload: dict) -> dict:
    """Gateway tools/call envelope around a payload the gateway already built."""
    response, _sid = handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "probe", "arguments": {}},
        },
        dispatch=lambda _name, _arguments: payload,
    )
    return response["result"]


def _json_line(record: dict) -> str:
    payload = {}
    for key in (
        "message", "service", "downstream", "downstream_service", "target",
        "change_type", "ci", "ci_name", "configuration_item", "exception", "level",
    ):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            payload[key] = val.strip()
    if "message" not in payload:
        desc = record.get("description") or record.get("short_description") or ""
        if isinstance(desc, str) and desc.strip():
            payload["message"] = desc.strip()
    return json.dumps(payload)


def _stamp(record: dict) -> str:
    for key in ("_time", "timestamp", "scheduled_start", "start_time"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "2024-11-04T07:59:00Z"


def _prose_logs(records: list[dict], *, limit: int | None = None) -> dict:
    """Loki lines whose text is the record message. shape_logs reads that text."""
    streams = []
    for rec in records:
        streams.append({
            "stream": {"service": str(rec.get("service") or "unknown"), "level": "error"},
            "values": [[_ns(_stamp(rec)), str(rec.get("message") or "")]],
        })
    body = {"status": "success", "data": {"resultType": "streams", "result": streams}}
    return shape_logs(body, limit=limit)


def _shaped_logs(
    records: list[dict],
    *,
    stream_for=None,
    limit: int | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    streams = []
    for rec in records:
        if stream_for is not None:
            label = stream_for(rec)
        else:
            label = str(rec.get("service") or "unknown")
        streams.append({
            "stream": {"service": label or "unknown", "level": "error"},
            "values": [[_ns(_stamp(rec)), _json_line(rec)]],
        })
    body = {"status": "success", "data": {"resultType": "streams", "result": streams}}
    return shape_logs(
        body, limit=limit, window_start=window_start, window_end=window_end,
    )


def _prom_series(metric: str, service: str, points: list[tuple[str, float]]) -> dict:
    body = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{
                "metric": {"service": service, "__name__": metric},
                "values": [[_unix(ts), str(value)] for ts, value in points],
            }],
        },
    }
    return shape_metrics(body, metric_name=metric, service=service)


def _merged_series(*shaped: dict) -> dict:
    points = []
    for payload in shaped:
        points.extend(payload["metrics"]["metrics"])
    return {
        "metrics": {"metrics": points, "baseline": points[0]["value"] if points else 0, "pattern": "none"},
        "source": "prometheus",
    }


def _summary_for(incident_type: str, service: str, summary: str = "") -> str:
    if summary:
        return summary
    if incident_type == "latency":
        return f"{service} latency"
    if incident_type == "timeout":
        return f"{service} timeout"
    return f"{service} error spike"


def _investigate(
    monkeypatch,
    *,
    mode: str,
    incident_type: str,
    service: str,
    logs: list[dict],
    changes: list[dict] | None = None,
    summary: str = "",
    incident_extra: dict | None = None,
    case_id: str = "INC-GATE",
    stream_for=None,
    log_body=None,
    change_body=None,
    metric_body=None,
    golden_body=None,
    start_time: str = _START,
):
    """One investigate() on all-logs, a tools/call envelope, or Loki lines."""
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("PARALLEL_PLAYBOOK", "false")
    monkeypatch.setenv("CALIBRATION_ENABLED", "false")
    monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")
    changes = list(changes or [])
    incident = {
        "id": case_id,
        "incident_id": case_id,
        "summary": _summary_for(incident_type, service, summary),
        "affected_service": service,
        "severity": "high",
        "start_time": start_time,
        "status": "open",
    }
    if incident_extra:
        incident.update(incident_extra)

    if log_body is None:
        if mode == "loki":
            log_body = _shaped_logs(list(logs) + changes, stream_for=stream_for)
        else:
            log_body = {"logs": {"results": [dict(row) for row in logs], "count": len(logs)}}
        if mode == "frozen":
            log_body = _tools_call(log_body)
    if change_body is None:
        if mode == "loki":
            change_body = {"changes": []}
        else:
            change_body = {"changes": [dict(row) for row in changes]}
            if mode == "frozen":
                change_body = _tools_call(change_body)
    if metric_body is None:
        metric_body = {"metrics": {"metrics": [], "baseline": 0}}
        if mode == "frozen":
            metric_body = _tools_call(metric_body)
    if golden_body is None:
        golden_body = {"signals": {}}

    sup = SentinalAISupervisor()
    sup._parallel_playbook = False

    class _Ops:
        def execute(self, action, params):
            if action == "get_incident_by_id":
                return {"incident": dict(incident)}
            return {}

    class _Logs:
        def execute(self, action, params):
            if action == "search_logs":
                return log_body
            if action in ("get_change_data", "get_change_records"):
                return change_body
            return {}

    class _Metrics:
        def execute(self, action, params):
            if action in ("query_metrics", "get_resource_metrics"):
                return metric_body
            if action in ("get_golden_signals", "check_latency"):
                return golden_body
            return {}

    class _Quiet:
        def execute(self, action, params):
            if action in ("get_golden_signals", "check_latency", "get_change_records"):
                if action == "get_change_records":
                    return change_body
                return golden_body
            return {}

    for name in list(sup.workers):
        sup.workers[name] = _Quiet()
    sup.workers["ops_worker"] = _Ops()
    sup.workers["log_worker"] = _Logs()
    sup.workers["metrics_worker"] = _Metrics()
    sup.workers["itsm_worker"] = _Logs()
    return sup.investigate(case_id)


def _published(result: dict) -> list[dict]:
    cause = result.get("cause") or {}
    symptom = result.get("symptom") or {}
    refs = []
    refs.extend(ref for ref in cause.get("evidence_refs") or [] if isinstance(ref, dict))
    refs.extend(ref for ref in symptom.get("evidence_refs") or [] if isinstance(ref, dict))
    for group in cause.get("contradictions") or []:
        if isinstance(group, dict):
            refs.extend(ref for ref in group.get("evidence_refs") or [] if isinstance(ref, dict))
    return refs


def _supported_cases():
    """The six regression causes, with the records the suite already uses."""
    slow = [
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
    ]
    exception = [{
        "_time": "2024-11-04T07:59:20Z",
        "service": "billing-api",
        "level": "ERROR",
        "message": "IllegalStateException while charging card",
    }]
    deploy_log = [{
        "_time": "2024-11-04T07:59:20Z",
        "service": "billing-api",
        "level": "ERROR",
        "message": "IllegalStateException in billing-api 4.8.2 while charging card",
    }]
    deploy_change = [{
        "change_type": "deployment",
        "service": "billing-api",
        "description": "release 4.8.2 of billing-api",
        "scheduled_start": "2024-11-04T07:50:00Z",
    }]
    timeout_pool = [
        {
            "_time": "2024-11-04T07:59:30Z",
            "service": "order-api",
            "level": "ERROR",
            "message": "ERROR timeout waiting for connection: inventory-db",
            "downstream": "inventory-db",
        },
        {
            "_time": "2024-11-04T07:59:20Z",
            "service": "inventory-db",
            "downstream": "inventory-db",
            "level": "ERROR",
            "message": "ERROR connection pool exhausted: 20/20 connections in use",
        },
    ]
    owner = [
        {
            "_time": "2024-11-04T07:59:10Z",
            "service": "fulfillment-api",
            "downstream": "ledger-db",
            "level": "ERROR",
            "message": "ERROR the call timed out",
        },
        {
            "_time": "2024-11-04T07:59:20Z",
            "service": "fulfillment-api",
            "downstream": "ledger-db",
            "level": "ERROR",
            "message": "connection pool exhausted",
        },
    ]
    pool_over = [
        {
            "_time": "2024-11-04T07:59:10Z",
            "service": "invoicing-api",
            "downstream": "invoice-store",
            "level": "ERROR",
            "message": "ERROR the call timed out",
        },
        {
            "_time": "2024-11-04T07:59:20Z",
            "service": "invoicing-api",
            "downstream": "invoice-store",
            "level": "ERROR",
            "message": "SocketTimeoutException: connection pool limit reached (slots=20)",
        },
    ]
    return [
        ("latency_slow", slow, None, ""),
        ("error_spike_exception", exception, None, ""),
        ("deploy_version", deploy_log, deploy_change, ""),
        ("timeout_pool", timeout_pool, None, ""),
        ("owner_latency", owner, None, ""),
        ("pool_over_exception", pool_over, None, ""),
    ]


def _overclaim_cases():
    no_mechanism = [{
        "_time": "2024-11-04T07:59:10Z",
        "service": "checkout-api",
        "level": "ERROR",
        "message": "ERROR upstream reset",
    }]
    conflict = [
        {
            "_time": "2024-11-04T07:59:30Z",
            "service": "order-api",
            "level": "ERROR",
            "message": "ERROR timeout waiting for connection: inventory-db",
            "downstream": "inventory-db",
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
    summary = [{
        "_time": "2024-11-04T07:59:30Z",
        "service": "order-api",
        "level": "INFO",
        "message": "INFO healthcheck ok",
    }]
    foreign_logs = [{
        "_time": "2024-11-04T07:59:20Z",
        "service": "ledger-quill",
        "level": "ERROR",
        "message": "ERROR upstream reset",
    }]
    foreign_change = [{
        "change_type": "deployment",
        "service": "other-billing",
        "ci": "other-billing",
        "description": "release 9.9.9 of other-billing",
        "scheduled_start": "2024-11-04T07:50:00Z",
    }]
    return [
        ("no_mechanism", "error_spike", "checkout-api", no_mechanism, None, "", ("disk", "pool", "IllegalState"), False),
        ("conflict", "timeout", "order-api", conflict, None, "", (), False),
        (
            "summary_only", "timeout", "order-api", summary, None,
            "order-api timeout connection pool exhausted on inventory-db",
            ("pool", "exhausted"), False,
        ),
        (
            "foreign_deploy", "error_spike", "ledger-quill", foreign_logs, foreign_change,
            "", ("other-billing", "9.9.9", "deployed"), True,
        ),
    ]


def _is_supported_hit(result: dict, expected: dict) -> bool:
    cause = result.get("cause") or {}
    return (
        cause.get("statement") == expected["statement"]
        and cause.get("confidence") == expected["confidence"]
        and result.get("confidence") == expected["confidence"]
        and result.get("root_cause") == expected["statement"]
    )


def _is_overclaim(result: dict, forbidden: tuple[str, ...], *, phrase: bool) -> bool:
    cause = result.get("cause") or {}
    statement = str(cause.get("statement") or "")
    if "UNKNOWN" not in statement or int(result.get("confidence") or 0) >= 60:
        return True
    root = str(result.get("root_cause") or "").lower()
    for word in forbidden:
        if word.lower() in root:
            return True
    if any(ref.get("signal") == "deployment" for ref in cause.get("evidence_refs") or []):
        return True
    if phrase and _PHRASE not in " ".join(cause.get("unknowns") or []):
        return True
    return False


def regression_counts(monkeypatch) -> dict:
    """Supported hits and overclaims for the three search paths."""
    counts = {}
    for mode in _MODES:
        supported = []
        missed = []
        for name, logs, changes, summary in _supported_cases():
            expected = EXPECTED[name]
            result = _investigate(
                monkeypatch,
                mode=mode,
                incident_type=expected["incident_type"],
                service=expected["service"],
                logs=logs,
                changes=changes,
                summary=summary,
                case_id=f"INC-SUP-{name}-{mode}",
                stream_for=(
                    (lambda _rec: "ledger-quill") if name == "foreign_deploy" and mode == "loki" else None
                ),
            )
            if _is_supported_hit(result, expected):
                supported.append(name)
            else:
                cause = result.get("cause") or {}
                missed.append(
                    f"{name}: got {cause.get('statement')!r} "
                    f"confidence={result.get('confidence')} "
                    f"want {expected['statement']!r} {expected['confidence']}"
                )
        over = []
        for name, incident_type, service, logs, changes, summary, forbidden, phrase in _overclaim_cases():
            stream_for = None
            if name == "foreign_deploy" and mode == "loki":
                # The stream label is the alerted service. The line names another owner.
                def stream_for(_rec, _service="ledger-quill"):
                    return _service
            result = _investigate(
                monkeypatch,
                mode=mode,
                incident_type=incident_type,
                service=service,
                logs=logs,
                changes=changes,
                summary=summary,
                case_id=f"INC-OVER-{name}-{mode}",
                stream_for=stream_for,
            )
            if _is_overclaim(result, forbidden, phrase=phrase):
                cause = result.get("cause") or {}
                over.append(
                    f"{name}: {cause.get('statement')!r} confidence={result.get('confidence')} "
                    f"unknowns={cause.get('unknowns')}"
                )
        counts[mode] = {
            "supported": len(supported),
            "supported_names": supported,
            "missed": missed,
            "overclaims": len(over),
            "overclaim_names": over,
        }
    return counts


def test_regression_suite_bar(monkeypatch):
    """Frozen-gateway and all-logs find all six. Loki finds at least four. No overclaims."""
    counts = regression_counts(monkeypatch)
    problems = []
    for mode in ("all_logs", "frozen"):
        if counts[mode]["supported"] != 6:
            problems.append(f"{mode} supported {counts[mode]['supported']}/6 {counts[mode]['missed']}")
        if counts[mode]["overclaims"]:
            problems.append(f"{mode} overclaims {counts[mode]['overclaim_names']}")
    if counts["loki"]["supported"] < 4:
        problems.append(f"loki supported {counts['loki']['supported']}/6 {counts['loki']['missed']}")
    if counts["loki"]["overclaims"]:
        problems.append(f"loki overclaims {counts['loki']['overclaim_names']}")
    assert not problems, "\n".join(problems)


def test_unmatched_change_stays_unknown_on_every_path(monkeypatch):
    """A change that is not an exact owner match stays UNKNOWN on every path."""
    logs = [{
        "_time": "2024-11-04T07:59:20Z",
        "service": "ledger-quill",
        "level": "ERROR",
        "message": "ERROR upstream reset",
    }]
    changes = [{
        "change_type": "deployment",
        "service": "other-billing",
        "ci": "other-billing",
        "description": "release 9.9.9 of other-billing",
        "scheduled_start": "2024-11-04T07:50:00Z",
    }]
    for incident_type in ("timeout", "latency", "error_spike"):
        for mode in _MODES:
            stream_for = (lambda _rec: "ledger-quill") if mode == "loki" else None
            result = _investigate(
                monkeypatch,
                mode=mode,
                incident_type=incident_type,
                service="ledger-quill",
                logs=logs,
                changes=changes,
                case_id=f"INC-C1-{incident_type}-{mode}",
                stream_for=stream_for,
            )
            cause = result["cause"]
            assert "UNKNOWN" in cause["statement"], (incident_type, mode, cause["statement"])
            assert result["confidence"] < 60, (incident_type, mode, result["confidence"])
            assert _PHRASE in cause["unknowns"], (incident_type, mode, cause["unknowns"])
            assert not any(ref.get("signal") == "deployment" for ref in cause["evidence_refs"])
            assert "other-billing" not in cause["statement"]
            assert "deployed" not in cause["statement"].lower()


def test_exception_survives_a_foreign_change_on_every_path(monkeypatch):
    logs = [{
        "_time": "2024-11-04T07:59:20Z",
        "service": "billing-api",
        "level": "ERROR",
        "message": "IllegalStateException while charging card",
    }]
    changes = [{
        "change_type": "deployment",
        "service": "other-billing",
        "ci": "other-billing",
        "description": "release 9.9.9 of other-billing",
        "scheduled_start": "2024-11-04T07:50:00Z",
    }]
    for mode in _MODES:
        stream_for = (lambda _rec: "billing-api") if mode == "loki" else None
        result = _investigate(
            monkeypatch,
            mode=mode,
            incident_type="error_spike",
            service="billing-api",
            logs=logs,
            changes=changes,
            case_id=f"INC-C1-KEEP-{mode}",
            stream_for=stream_for,
        )
        cause = result["cause"]
        assert cause["statement"] == "IllegalStateException in billing-api", (mode, cause["statement"])
        assert result["confidence"] == 62, (mode, result["confidence"])
        assert not any(ref.get("signal") == "deployment" for ref in cause["evidence_refs"])
        assert "other-billing" not in cause["statement"]


def _pool_line(downstream: str, when: str = "2024-08-01T12:00:12Z") -> dict:
    return {
        "_time": when,
        "service": "ledger-quill",
        "level": "ERROR",
        "message": f"ERROR connection pool exhausted downstream={downstream}",
    }


def _timeout_line(message: str, when: str = "2024-08-01T12:00:10Z") -> dict:
    return {
        "_time": when,
        "service": "ledger-quill",
        "level": "ERROR",
        "message": message,
    }


def _c2(monkeypatch, records, *, incident_extra=None, case_id="INC-C2"):
    shaped = _prose_logs(records)
    # Prose lines, not a second copy of the shaped dict: the gateway built this.
    wrapped = _tools_call(shaped)
    return _investigate(
        monkeypatch,
        mode="frozen",
        incident_type="timeout",
        service="ledger-quill",
        logs=[],
        summary="ledger-quill timeout",
        incident_extra=incident_extra,
        case_id=case_id,
        log_body=wrapped,
        start_time="2024-08-01T12:00:00Z",
    )


def _dependency_of(result: dict, downstream: str) -> bool:
    from supervisor.helpers.timeout_evidence import _is_pool

    for ref in _published(result):
        if ref.get("signal") == "connection_pool_exhausted":
            continue
        record = resolve_evidence_ref(ref, result.get("evidence") or {}, result.get("receipts"))
        if not isinstance(record, dict) or _is_pool(record):
            continue
        if str(record.get("downstream") or record.get("target") or "") == downstream:
            return True
    return False


def test_cited_gateway_timeout_establishes_the_pool(monkeypatch):
    result = _c2(monkeypatch, [
        _timeout_line("ERROR timeout waiting for connection: cache-zeta"),
        _pool_line("cache-zeta"),
    ], case_id="INC-C2-BIND")
    cause = result["cause"]
    assert cause["category"] == "connection_pool_exhaustion", cause
    assert cause["confidence"] == 62
    assert result["confidence"] == 62
    assert "cache-zeta" in cause["statement"]
    assert "failing dependency not identified" not in cause["unknowns"]
    assert _dependency_of(result, "cache-zeta"), _published(result)


def test_pool_line_from_the_gateway_is_unknown(monkeypatch):
    result = _c2(monkeypatch, [_pool_line("cache-zeta")], case_id="INC-C2-ALONE")
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"]
    assert result["confidence"] < 60
    assert "failing dependency not identified" in cause["unknowns"]


def test_other_dependency_from_the_gateway_is_unknown(monkeypatch):
    result = _c2(monkeypatch, [
        _timeout_line("ERROR timeout waiting for connection: cache-zeta"),
        _pool_line("mint-ledger"),
    ], case_id="INC-C2-OTHER")
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"]
    assert "failing dependency not identified" in cause["unknowns"]
    assert result["confidence"] < 60


def test_message_text_from_the_gateway_does_not_establish(monkeypatch):
    result = _c2(monkeypatch, [
        _timeout_line("upstream request timeout: vellum-cache:8080 (31000ms)"),
        _pool_line("vellum-cache"),
    ], case_id="INC-C2-TEXT")
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"]
    assert "failing dependency not identified" in cause["unknowns"]
    assert result["confidence"] < 60
    assert not _dependency_of(result, "vellum-cache")


def test_out_of_window_dependency_is_not_cited(monkeypatch):
    """The naming line was retrieved. It is outside the window, so it is not cited."""
    result = _c2(monkeypatch, [
        _timeout_line(
            "ERROR timeout waiting for connection: cache-zeta",
            when="2024-08-01T10:00:00Z",
        ),
        _pool_line("cache-zeta"),
    ], case_id="INC-C2-WINDOW")
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"]
    assert "failing dependency not identified" in cause["unknowns"]
    assert result["confidence"] < 60
    assert not _dependency_of(result, "cache-zeta")


def test_alert_field_binds_the_gateway_pool(monkeypatch):
    result = _c2(
        monkeypatch,
        [_pool_line("vellum-cache")],
        incident_extra={"downstream": "vellum-cache"},
        case_id="INC-C2-ALERT",
    )
    cause = result["cause"]
    assert cause["category"] == "connection_pool_exhaustion", cause
    assert "vellum-cache" in cause["statement"]
    assert cause["confidence"] == 62
    assert "failing dependency not identified" not in cause["unknowns"]


@pytest.mark.parametrize("named,pool_dep", [
    ("ledger-db", "ledger-db-replica"),
    ("ledger-db-replica", "ledger-db"),
    ("ledger", "ledger-db"),
    ("ledger-db", "ledger"),
])
def test_suffix_confusable_dependency_does_not_bind(monkeypatch, named, pool_dep):
    """Exact match only. A longer or shorter name is a different dependency."""
    result = _c2(
        monkeypatch,
        [
            _timeout_line(f"ERROR timeout waiting for connection: {named}"),
            _pool_line(pool_dep),
        ],
        case_id=f"INC-C2-SUFFIX-{named}-{pool_dep}",
    )
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"], (named, pool_dep, cause)
    assert "failing dependency not identified" in cause["unknowns"]
    assert result["confidence"] < 60
    assert cause["category"] != "connection_pool_exhaustion"


def test_exact_dependency_name_still_binds(monkeypatch):
    result = _c2(
        monkeypatch,
        [
            _timeout_line("ERROR timeout waiting for connection: ledger-db"),
            _pool_line("ledger-db"),
        ],
        case_id="INC-C2-EXACT",
    )
    cause = result["cause"]
    assert cause["category"] == "connection_pool_exhaustion", cause
    assert "ledger-db" in cause["statement"]
    assert cause["confidence"] == 62


def test_unnamed_dependency_does_not_bind(monkeypatch):
    result = _c2(
        monkeypatch,
        [
            _timeout_line("ERROR timeout waiting for connection"),
            {
                "_time": "2024-08-01T12:00:12Z",
                "service": "ledger-quill",
                "level": "ERROR",
                "message": "ERROR connection pool exhausted",
            },
        ],
        case_id="INC-C2-UNNAMED",
    )
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"]
    assert "failing dependency not identified" in cause["unknowns"]
    assert result["confidence"] < 60


def test_alert_field_rejects_a_different_gateway_pool(monkeypatch):
    result = _c2(
        monkeypatch,
        [_pool_line("brine-ledger")],
        incident_extra={"downstream": "vellum-cache"},
        case_id="INC-C2-ALERT-NO",
    )
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"]
    assert "failing dependency not identified" in cause["unknowns"]
    assert result["confidence"] < 60


def _pool_logs():
    return _tools_call(_prose_logs([
        _timeout_line("ERROR timeout waiting for connection: cache-zeta"),
        _pool_line("cache-zeta"),
    ]))


def _resolved(ref: dict, result: dict):
    return resolve_evidence_ref(ref, {}, result.get("receipts"))


def test_below_limit_series_is_a_contradiction(monkeypatch):
    """Gateway usage stays well below the pool limit. It is not support."""
    active = _prom_series("db_connection_pool_active", "ledger-quill", [
        ("2024-08-01T12:00:00Z", 4),
        ("2024-08-01T12:00:30Z", 5),
        ("2024-08-01T12:01:00Z", 4),
    ])
    limit = _prom_series("db_connection_pool_max", "ledger-quill", [
        ("2024-08-01T12:00:00Z", 50),
        ("2024-08-01T12:00:30Z", 50),
        ("2024-08-01T12:01:00Z", 50),
    ])
    logs = _pool_logs()
    common = dict(
        mode="frozen",
        incident_type="timeout",
        service="ledger-quill",
        logs=[],
        summary="ledger-quill timeout",
        log_body=logs,
        start_time="2024-08-01T12:00:00Z",
    )
    baseline = _investigate(monkeypatch, case_id="INC-CONTRA-BASE", **common)
    assert baseline["cause"]["category"] == "connection_pool_exhaustion"
    assert baseline["confidence"] == 62
    result = _investigate(
        monkeypatch,
        case_id="INC-CONTRA",
        metric_body=_tools_call(_merged_series(active, limit)),
        **common,
    )
    cause = result["cause"]
    assert "UNKNOWN" in cause["statement"], cause
    assert result["confidence"] < 60
    assert result["confidence"] < baseline["confidence"]
    text = " ".join(group.get("statement", "") for group in cause["contradictions"])
    assert "pool not saturated" in text
    assert "active 5" in text or "active 4" in text
    assert "max 50" in text
    paired = False
    for group in cause["contradictions"]:
        refs = [ref for ref in group.get("evidence_refs") or [] if isinstance(ref, dict)]
        records = [_resolved(ref, result) for ref in refs]
        has_pool = any(
            isinstance(row, dict) and "pool" in str(row.get("message") or row.get("_raw") or "").lower()
            for row in records
        )
        has_series = any(
            isinstance(row, dict)
            and isinstance(row.get("value"), (int, float))
            and not isinstance(row.get("value"), bool)
            and float(row["value"]) <= 25
            and "pool" in str(row.get("metric") or row.get("name") or "")
            for row in records
        )
        if has_pool and has_series:
            paired = True
    assert paired, cause["contradictions"]
    for ref in cause.get("evidence_refs") or []:
        row = _resolved(ref, result)
        metric = str((row or {}).get("metric") or (row or {}).get("name") or "")
        assert "db_connection_pool_active" not in metric
    coverage = result["_confidence_provenance"]["unchecked_coverage"]
    assert not any(row.get("reason") == "unparsed_format" for row in coverage["unavailable_signals"])


def test_golden_signal_neither_supports_nor_contradicts(monkeypatch):
    """Saturation here is CPU. The summary is the detector's conclusion."""
    golden = shape_golden_signals(
        {"latency_p95": 80.0, "error_rate": 0.01, "saturation": 12.0},
        service="ledger-quill",
    )
    golden["signals"]["summary"] = "detector: cpu saturation within bounds"
    assert golden["signals"]["golden_signals"]["saturation"]["pct"] == 12.0
    logs = _pool_logs()
    common = dict(
        mode="frozen",
        incident_type="timeout",
        service="ledger-quill",
        logs=[],
        summary="ledger-quill timeout",
        log_body=logs,
        start_time="2024-08-01T12:00:00Z",
    )
    plain = _investigate(monkeypatch, case_id="INC-GOLDEN-BASE", **common)
    marked = _investigate(
        monkeypatch,
        case_id="INC-GOLDEN-CPU",
        golden_body=_tools_call(golden),
        **common,
    )
    assert marked["cause"]["category"] == "connection_pool_exhaustion"
    assert marked["confidence"] == plain["confidence"] == 62
    text = " ".join(
        group.get("statement", "") for group in marked["cause"].get("contradictions") or []
    )
    assert "pool not saturated" not in text
    assert "cpu" not in marked["cause"]["statement"].lower()
    for ref in marked["cause"].get("evidence_refs") or []:
        row = _resolved(ref, marked) or {}
        blob = " ".join(str(row.get(key) or "") for key in ("metric", "name", "summary"))
        assert "saturation" not in blob.lower()


def test_series_that_reaches_the_limit_does_not_contradict(monkeypatch):
    active = _prom_series("db_connection_pool_active", "ledger-quill", [
        ("2024-08-01T12:00:00Z", 10),
        ("2024-08-01T12:00:30Z", 35),
        ("2024-08-01T12:01:00Z", 50),
    ])
    limit = _prom_series("db_connection_pool_max", "ledger-quill", [
        ("2024-08-01T12:00:00Z", 50),
        ("2024-08-01T12:00:30Z", 50),
        ("2024-08-01T12:01:00Z", 50),
    ])
    result = _investigate(
        monkeypatch,
        mode="frozen",
        incident_type="timeout",
        service="ledger-quill",
        logs=[],
        summary="ledger-quill timeout",
        case_id="INC-HIT-LIMIT",
        log_body=_tools_call(_prose_logs([
            _timeout_line("ERROR timeout waiting for connection: cache-zeta"),
            _pool_line("cache-zeta"),
        ])),
        metric_body=_tools_call(_merged_series(active, limit)),
        start_time="2024-08-01T12:00:00Z",
    )
    cause = result["cause"]
    assert cause["category"] == "connection_pool_exhaustion", cause
    assert result["confidence"] == 62
    text = " ".join(group.get("statement", "") for group in cause.get("contradictions") or [])
    assert "pool not saturated" not in text
    for ref in cause.get("evidence_refs") or []:
        row = _resolved(ref, result) or {}
        metric = str(row.get("metric") or row.get("name") or "")
        assert "db_connection_pool" not in metric


def _irrelevant_series_stays_at_the_log_cause(monkeypatch, metric_body, case_id):
    """A series that is not the owner's in-window pool does not move the cause."""
    common = dict(
        mode="frozen",
        incident_type="timeout",
        service="ledger-quill",
        logs=[],
        summary="ledger-quill timeout",
        log_body=_pool_logs(),
        start_time="2024-08-01T12:00:00Z",
    )
    baseline = _investigate(monkeypatch, case_id=f"{case_id}-BASE", **common)
    marked = _investigate(
        monkeypatch,
        case_id=case_id,
        metric_body=_tools_call(metric_body),
        **common,
    )
    assert baseline["cause"]["category"] == "connection_pool_exhaustion"
    assert baseline["confidence"] == 62
    assert marked["cause"]["category"] == "connection_pool_exhaustion"
    assert marked["cause"]["statement"] == baseline["cause"]["statement"]
    assert marked["confidence"] == baseline["confidence"]
    text = " ".join(
        group.get("statement", "") for group in marked["cause"].get("contradictions") or []
    )
    assert "pool not saturated" not in text
    for ref in marked["cause"].get("evidence_refs") or []:
        row = _resolved(ref, marked) or {}
        metric = str(row.get("metric") or row.get("name") or "")
        assert "db_connection_pool" not in metric
        assert "cpu_usage" not in metric


def test_other_service_series_neither_supports_nor_contradicts(monkeypatch):
    active = _prom_series("db_connection_pool_active", "brine-ledger", [
        ("2024-08-01T12:00:00Z", 4),
        ("2024-08-01T12:00:30Z", 5),
        ("2024-08-01T12:01:00Z", 4),
    ])
    limit = _prom_series("db_connection_pool_max", "brine-ledger", [
        ("2024-08-01T12:00:00Z", 50),
        ("2024-08-01T12:00:30Z", 50),
        ("2024-08-01T12:01:00Z", 50),
    ])
    _irrelevant_series_stays_at_the_log_cause(
        monkeypatch, _merged_series(active, limit), "INC-CONTRA-OTHER-SVC",
    )


def test_other_resource_series_neither_supports_nor_contradicts(monkeypatch):
    cpu = _prom_series("cpu_usage_percent", "ledger-quill", [
        ("2024-08-01T12:00:00Z", 12),
        ("2024-08-01T12:00:30Z", 14),
        ("2024-08-01T12:01:00Z", 11),
    ])
    _irrelevant_series_stays_at_the_log_cause(monkeypatch, cpu, "INC-CONTRA-CPU")


def test_out_of_window_series_neither_supports_nor_contradicts(monkeypatch):
    active = _prom_series("db_connection_pool_active", "ledger-quill", [
        ("2024-08-01T10:00:00Z", 4),
        ("2024-08-01T10:00:30Z", 5),
        ("2024-08-01T10:01:00Z", 4),
    ])
    limit = _prom_series("db_connection_pool_max", "ledger-quill", [
        ("2024-08-01T10:00:00Z", 50),
        ("2024-08-01T10:00:30Z", 50),
        ("2024-08-01T10:01:00Z", 50),
    ])
    _irrelevant_series_stays_at_the_log_cause(
        monkeypatch, _merged_series(active, limit), "INC-CONTRA-WINDOW",
    )


def test_unknown_metric_from_dispatch_is_unparsed(monkeypatch):
    """dispatch() builds the unread body. investigate() has to say so."""
    payload = dispatch(
        "sysdig.query_metrics",
        {"metric": "db_connection_pool_active", "service": "ledger-quill"},
        Backends(settings=Settings()),
    )
    assert "unknown_metric" in str(payload.get("error"))
    slow = {
        "_time": "2024-08-01T12:00:10Z",
        "service": "ledger-quill",
        "level": "ERROR",
        "message": "slow query on ledger-quill took 4200ms",
    }
    plain = _investigate(
        monkeypatch,
        mode="all_logs",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-PLAIN",
        start_time="2024-08-01T12:00:00Z",
        metric_body={"metrics": {"metrics": [], "baseline": 0}},
    )
    marked = _investigate(
        monkeypatch,
        mode="frozen",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-UNPARSED",
        start_time="2024-08-01T12:00:00Z",
        log_body=_tools_call({"logs": {"results": [slow], "count": 1}}),
        metric_body=_tools_call(payload),
    )
    assert marked["confidence"] <= plain["confidence"]
    assert marked["cause"]["statement"] == plain["cause"]["statement"]
    coverage = marked["_confidence_provenance"]["unchecked_coverage"]
    rows = [row for row in coverage["unavailable_signals"] if row.get("reason") == "unparsed_format"]
    assert len(rows) == 1
    assert rows[0]["signal"] == "db_connection_pool_active"
    query_id = rows[0]["query_id"]
    assert query_id
    unknowns = " ".join(marked["cause"]["unknowns"])
    assert f"metric not read: db_connection_pool_active unparsed_format query_id={query_id}" in unknowns
    assert all(ref.get("query_id") != query_id for ref in _published(marked))
    golden = shape_golden_signals(
        {"latency_p95": 80.0, "error_rate": 0.01},
        service="ledger-quill",
    )
    readable = _investigate(
        monkeypatch,
        mode="frozen",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-GOLDEN",
        start_time="2024-08-01T12:00:00Z",
        log_body=_tools_call({"logs": {"results": [slow], "count": 1}}),
        metric_body=_tools_call(golden),
    )
    readable_rows = [
        row for row in readable["_confidence_provenance"]["unchecked_coverage"]["unavailable_signals"]
        if row.get("reason") == "unparsed_format"
    ]
    assert readable_rows == []
    empty = shape_metrics(
        {"status": "success", "data": {"resultType": "matrix", "result": []}},
        metric_name="db_connection_pool_active",
        service="ledger-quill",
    )
    absent = _investigate(
        monkeypatch,
        mode="frozen",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-EMPTY",
        start_time="2024-08-01T12:00:00Z",
        log_body=_tools_call({"logs": {"results": [slow], "count": 1}}),
        metric_body=_tools_call(empty),
    )
    empty_rows = [
        row for row in absent["_confidence_provenance"]["unchecked_coverage"]["unavailable_signals"]
        if row.get("reason") == "unparsed_format"
    ]
    assert empty_rows == []
    absent_rows = [
        row for row in absent["_confidence_provenance"]["unchecked_coverage"]["unavailable_signals"]
        if row.get("reason") == "absent"
    ]
    assert absent_rows
    assert absent_rows[0]["evidence_key"]
    assert absent_rows[0]["query_id"]
    absent_unknowns = " ".join(absent["cause"]["unknowns"])
    assert f"metric absent: {absent_rows[0]['signal']} query_id={absent_rows[0]['query_id']}" in absent_unknowns
    assert "unparsed_format" not in absent_unknowns
    assert rows[0]["reason"] == "unparsed_format"
    assert rows[0]["evidence_key"]
    assert rows[0]["query_id"]
    marked_absent = {
        row.get("query_id")
        for row in marked["_confidence_provenance"]["unchecked_coverage"]["unavailable_signals"]
        if row.get("reason") == "absent"
    }
    assert rows[0]["query_id"] not in marked_absent


_DATADOG = {
    "status": "ok",
    "res_type": "time_series",
    "series": [{
        "metric": "ledger.quill.pool.active",
        "pointlist": [[1722506410000, 4.0], [1722506440000, 5.0]],
        "scope": "service:ledger-quill",
    }],
}
_DYNATRACE = {
    "totalCount": 1,
    "result": [{
        "metricId": "builtin:service.pool.used",
        "data": [{"timestamps": [1722506410000], "values": [4.0]}],
    }],
}
_SPLUNK_JSON = {
    "preview": False,
    "init_offset": 0,
    "results": [{
        "_time": "2024-08-01T12:00:10Z",
        "metric_name": "pool_active",
        "_raw": "pool_active=4",
    }],
}
_SPLUNK_XML = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
    "<results preview=\"0\"><result>"
    "<field k=\"metric_name\"><value><text>pool_active</text></value></field>"
    "</result></results>"
)
_TRUNCATED_JSON = '{"series": [{"metric": "ledger.quill.pool.active", "pointlist": ['
_UNEXPECTED = {"widget": "gauge", "bands": ["green", "red"], "reading": 4}


@pytest.mark.parametrize("body,signal", [
    (_DATADOG, "ledger.quill.pool.active"),
    (_DYNATRACE, "builtin:service.pool.used"),
    (_SPLUNK_JSON, ""),
    (_SPLUNK_XML, ""),
    (_TRUNCATED_JSON, ""),
    (_UNEXPECTED, ""),
])
def test_metrics_path_vendor_body_is_unparsed(monkeypatch, body, signal):
    """A non-gateway body on the metrics path is unparsed, values unread."""
    slow = {
        "_time": "2024-08-01T12:00:10Z",
        "service": "ledger-quill",
        "level": "ERROR",
        "message": "slow query on ledger-quill took 4200ms",
    }
    plain = _investigate(
        monkeypatch,
        mode="all_logs",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-VENDOR-PLAIN",
        start_time="2024-08-01T12:00:00Z",
    )
    # The metrics worker returns this body on query_metrics. A dict is the
    # gateway tools/call envelope. A string is the raw body on that same path.
    metric_body = _tools_call(body) if isinstance(body, dict) else body
    marked = _investigate(
        monkeypatch,
        mode="all_logs",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-VENDOR",
        start_time="2024-08-01T12:00:00Z",
        metric_body=metric_body,
    )
    assert marked["confidence"] <= plain["confidence"]
    assert marked["cause"]["statement"] == plain["cause"]["statement"]
    coverage = marked["_confidence_provenance"]["unchecked_coverage"]["unavailable_signals"]
    rows = [row for row in coverage if row.get("reason") == "unparsed_format"]
    assert rows
    if signal:
        assert any(row.get("signal") == signal for row in rows)
    unknowns = " ".join(marked["cause"]["unknowns"])
    published = {ref.get("query_id") for ref in _published(marked)}
    metric_ids = {
        receipt.get("query_id")
        for receipt in marked["receipts"]
        if receipt.get("action") in (
            "query_metrics", "get_resource_metrics",
            "get_golden_signals", "check_latency",
        )
    }
    log_ids = {
        receipt.get("query_id")
        for receipt in marked["receipts"]
        if receipt.get("action") == "search_logs"
    }
    for row in rows:
        assert row["evidence_key"]
        assert row["query_id"]
        assert row["query_id"] in metric_ids
        assert row["query_id"] not in log_ids
        assert row["query_id"] not in published
        assert f"metric not read: {row['signal']} unparsed_format query_id={row['query_id']}" in unknowns
    assert all(row.get("reason") != "unparsed_format" for row in coverage if row.get("reason") == "absent")


def test_vendor_log_search_is_outside_unparsed_metrics(monkeypatch):
    """The same Splunk body on the logs path is not an unparsed metric."""
    slow = {
        "_time": "2024-08-01T12:00:10Z",
        "service": "ledger-quill",
        "level": "ERROR",
        "message": "slow query on ledger-quill took 4200ms",
    }
    result = _investigate(
        monkeypatch,
        mode="all_logs",
        incident_type="latency",
        service="ledger-quill",
        logs=[slow],
        summary="ledger-quill latency",
        case_id="INC-C3-LOG-VENDOR",
        start_time="2024-08-01T12:00:00Z",
        log_body=dict(_SPLUNK_JSON),
        metric_body={"metrics": {"metrics": [], "baseline": 0}},
    )
    coverage = result["_confidence_provenance"]["unchecked_coverage"]["unavailable_signals"]
    assert not any(row.get("reason") == "unparsed_format" for row in coverage)
    assert any(row.get("reason") == "absent" for row in coverage)


@pytest.mark.parametrize("incident_type,summary,needle", [
    ("timeout", "ledger-quill timeout", "pool"),
    ("latency", "ledger-quill latency", "slow"),
    ("error_spike", "ledger-quill error spike", "error"),
])
@pytest.mark.parametrize("case", ["requested", "unwindowed", "mismatch"])
def test_capped_query_gap_uses_the_requested_window(
    monkeypatch, incident_type, summary, needle, case,
):
    """The gap keeps the window the query asked for, not the one the search returned."""
    requested = ("2024-08-01T11:45:00Z", "2024-08-01T12:15:00Z")
    reported = ("2024-08-01T10:00:00Z", "2024-08-01T10:30:00Z")
    if case == "requested":
        returned = requested
    else:
        returned = reported
    line = _shaped_logs([
        {
            "_time": "2024-08-01T12:00:10Z",
            "service": "ledger-quill",
            "level": "ERROR",
            "message": "slow query on ledger-quill took 4200ms",
        }
    ], limit=1, window_start=returned[0], window_end=returned[1])
    assert line["truncated"] is True
    assert line["window_start"] == returned[0]
    full = _shaped_logs([
        {
            "_time": "2024-08-01T12:00:10Z",
            "service": "ledger-quill",
            "level": "ERROR",
            "message": "ERROR ledger-quill request failed",
        }
    ], limit=5, window_start="2024-08-01T11:40:00Z", window_end="2024-08-01T12:20:00Z")
    assert full["truncated"] is False

    def choose(action, params):
        query = str((params or {}).get("query") or "")
        if action == "search_logs" and needle in query:
            return line
        if action == "search_logs":
            return full
        return {"changes": []}

    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("PARALLEL_PLAYBOOK", "false")
    monkeypatch.setenv("CALIBRATION_ENABLED", "false")
    incident = {
        "id": f"INC-C4-{incident_type}-{case}",
        "summary": summary,
        "affected_service": "ledger-quill",
        "severity": "high",
        "start_time": "2024-08-01T12:00:00Z",
        "status": "open",
    }
    sup = SentinalAISupervisor()
    sup._parallel_playbook = False
    original = sup._build_params

    def build_params(step, incident_id, service):
        params = original(step, incident_id, service)
        if case != "unwindowed" and step.get("action") == "search_logs":
            if needle in str(params.get("query") or ""):
                params["start_time"] = requested[0]
                params["end_time"] = requested[1]
        return params

    sup._build_params = build_params

    class _Ops:
        def execute(self, action, params):
            if action == "get_incident_by_id":
                return {"incident": dict(incident)}
            return {}

    class _Logs:
        def execute(self, action, params):
            return choose(action, params)

    class _Quiet:
        def execute(self, action, params):
            if action in ("get_golden_signals", "check_latency"):
                return {"signals": {}}
            if action in ("query_metrics", "get_resource_metrics"):
                return {"metrics": {"metrics": [], "baseline": 0}}
            return {}

    for name in list(sup.workers):
        sup.workers[name] = _Quiet()
    sup.workers["ops_worker"] = _Ops()
    sup.workers["log_worker"] = _Logs()
    result = sup.investigate(incident["id"])
    gaps = result["_confidence_provenance"]["unchecked_coverage"]["query_gaps"]
    assert len(gaps) == 1, (incident_type, case, gaps)
    gap = gaps[0]
    receipts = [row for row in result["receipts"] if row.get("action") == "search_logs"]
    capped = next(row for row in receipts if needle in str(row.get("filter") or ""))
    others = [row for row in receipts if row is not capped]
    assert capped.get("truncated") is True
    assert gap["query_id"] == capped["query_id"]
    assert gap["query_id"]
    assert gap["signal"]
    assert needle in gap["signal"]
    assert gap["truncated"] is True
    assert all(gap["query_id"] != row.get("query_id") for row in others)
    unknowns = " ".join(result["cause"]["unknowns"])
    asked = None if case == "unwindowed" else {"start": requested[0], "end": requested[1]}
    if asked is None:
        disclosure = f"capped query: {gap['signal']} query_id={gap['query_id']}"
    else:
        disclosure = (
            f"capped query: {gap['signal']} query_id={gap['query_id']} "
            f"window {asked['start']} to {asked['end']}"
        )
    # Recorded for this case: the window the query asked for, the cap,
    # the query id, and the disclosure the final output must carry.
    recorded = {
        "requested_window": asked,
        "cap": {"truncated": line["truncated"], "limit": line["limit"]},
        "query_id": gap["query_id"],
        "disclosure": disclosure,
    }
    assert recorded["cap"] == {"truncated": True, "limit": 1}
    assert recorded["query_id"] == capped["query_id"]
    assert recorded["disclosure"] in unknowns
    assert "window  to " not in unknowns
    if recorded["requested_window"] is None:
        assert "window_start" not in gap
        assert "window_end" not in gap
        assert reported[0] not in unknowns
        assert "search did not report the window it covered" in unknowns
    else:
        assert gap["window_start"] == recorded["requested_window"]["start"]
        assert gap["window_end"] == recorded["requested_window"]["end"]
        if case == "mismatch":
            assert gap["window_start"] != returned[0]
            assert returned[0] not in unknowns
