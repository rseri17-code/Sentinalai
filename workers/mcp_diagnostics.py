"""Connection diagnostics for each worker's MCP server.

One state per worker, sorted by worker name:

- reachable: the endpoint answered within the probe timeout and lists every
  tool that worker calls, and a read-only probe is not an honest skip.
- stubbed: fixtures, because GATEWAY_MODE=stub or nothing is configured.
- missing: this worker has no endpoint, or the endpoint is up but does not
  provide a required tool (unlisted, or listed only as ``skipped: true``).
- failed: the probe errored, timed out, was rejected after one 401 retry,
  or the MCP client could not be built.

A tool the OSS validation shim advertises but answers with ``skipped: true``
is not provided. That is missing, not stubbed and not reachable. The client
does not reimplement the shim; it only reads the payload the shim already
returns.

The report is deterministic for the same inputs: workers are sorted, reasons
are sorted, and the report has no timestamps. Secrets are scrubbed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from functools import partial
from typing import Any, Callable

from workers.mcp_client import (
    MCP_TOOL_ARNS,
    McpGateway,
    _call_with_timeout,
    _has_any_arn,
    _is_unauthorized,
    force_stub_gateway,
    mcp_call_timeout_seconds,
    normalize_mcp_result,
    outbound_tool_name,
    plain_mcp_enabled,
    resolved_gateway_url,
    scrub_secrets,
    tool_identity,
    tool_names_from_list,
)

# Tools each worker actually calls on its declared servers.
# Read from the worker modules; not the full server catalog (a catalog entry
# the worker never calls, such as moogsoft.get_historical_analysis, must not
# flip ops_worker to missing).
WORKER_TOOLS: dict[str, tuple[str, ...]] = {
    "ops_worker": ("moogsoft.get_incident_by_id",),
    "log_worker": ("splunk.search_oneshot", "splunk.get_change_data"),
    "metrics_worker": ("sysdig.query_metrics", "sysdig.get_events"),
    "apm_worker": ("dynatrace.get_metrics", "signalfx.query_signalfx_metrics"),
    "signal_worker": ("dynatrace.get_metrics", "signalfx.query_signalfx_metrics"),
    "event_worker": ("dynatrace.get_metrics", "signalfx.query_signalfx_metrics"),
    "itsm_worker": (
        "servicenow.get_ci_details",
        "servicenow.search_incidents",
        "servicenow.get_change_records",
        "servicenow.get_known_errors",
    ),
    "devops_worker": (
        "github.get_recent_deployments",
        "github.get_pr_details",
        "github.get_commit_diff",
        "github.get_workflow_runs",
    ),
    "change_worker": (
        "github.get_recent_deployments",
        "github.get_pr_details",
        "github.get_commit_diff",
        "github.get_workflow_runs",
    ),
    "code_worker": (
        "github.get_recent_deployments",
        "github.get_commit_diff",
    ),
    "git_worker": (
        "github.git_log",
        "github.git_blame",
        "github.git_diff",
        "github.git_show",
        "github.get_pr_for_commit",
    ),
    "confluence_worker": (
        "confluence.search_runbooks",
        "confluence.search_postmortems",
        "confluence.get_page",
    ),
    "knowledge_worker": (),
    "network_worker": (),
}

# Diagnostics must not invoke mutations.
_MUTATING_TOOLS = frozenset({
    "github.create_fix_pr",
    "github.create_pull_request",
    "github.reply_to_review_comment",
    "github.request_reviewers",
    "kubernetes.rollback_deployment",
    "kubernetes.scale_service",
    "servicenow.update_incident",
})

_EVIDENCE_KEYS = frozenset({
    "incident", "incidents", "logs", "metrics", "signals", "events",
    "changes", "alerts", "problems", "ci", "deployments", "pr", "commit",
    "workflow_runs", "runbooks", "page", "entities", "resources",
})

HASH_EXCLUSIONS: tuple[str, ...] = (
    "timestamps (timestamp, time_window_*, wall_clock_*, start_ts, end_ts, generated_at)",
    "durations (elapsed, latency, duration)",
    "UUIDs and trace ids (uuid, trace_id, correlation_id, report_id, span)",
    "model text (reasoning, rca_markdown, llm narrative)",
)

_DROP_KEY_FRAGMENTS = (
    "timestamp", "time_window", "wall_clock", "elapsed", "latency",
    "duration", "uuid", "trace_id", "correlation", "generated_at",
    "report_id", "span_id",
)
_DROP_KEYS = frozenset({"ts", "_time", "start_ts", "end_ts"})


ListTools = Callable[[], list[str]]
CallTool = Callable[[str], Any]


def worker_servers() -> dict[str, frozenset[str]]:
    """Worker → required servers, from the supervisor table."""
    from supervisor.agent import SentinalAISupervisor
    return {name: frozenset(servers) for name, servers in SentinalAISupervisor._WORKER_SERVERS.items()}


def _tools_for(worker: str, servers: frozenset[str], override: dict[str, tuple[str, ...]] | None) -> tuple[str, ...]:
    if override is not None and worker in override:
        return override[worker]
    if worker in WORKER_TOOLS:
        return WORKER_TOOLS[worker]
    from workers.mcp_client import _TOOL_TO_SERVER
    return tuple(sorted(name for name, server in _TOOL_TO_SERVER.items() if server in servers))


def _endpoint_for(server: str) -> bool:
    if resolved_gateway_url():
        return True
    return bool(MCP_TOOL_ARNS.get(server, ""))


def _has_endpoint(server: str, assume: bool | None) -> bool:
    if assume is not None:
        return assume
    return _endpoint_for(server)


def _row(
    worker: str,
    servers: frozenset[str],
    state: str,
    reason: str,
    error_class: str | None = None,
) -> dict[str, Any]:
    return {
        "worker": worker,
        "state": state,
        "required": bool(servers),
        "servers": sorted(servers),
        "reason": scrub_secrets(reason),
        "error_class": error_class,
    }


def _listed_identities(names: list[str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for name in names:
        ident = tool_identity(name)
        if ident is not None:
            found.add(ident)
        else:
            found.add(("raw", name))
    return found


def _is_listed(tool: str, found: set[tuple[str, str]]) -> bool:
    ident = tool_identity(tool)
    if ident is not None and ident in found:
        return True
    return ("raw", tool) in found


def _probe_is_failure(payload: Any) -> str | None:
    """Return an error class when a probe payload is a hard failure."""
    if not isinstance(payload, dict):
        return None
    if payload.get("skipped") is True:
        return None
    if payload.get("connection_state") == "failed" or payload.get("error_class"):
        return str(payload.get("error_class") or "Error")
    if payload.get("error") and not (_EVIDENCE_KEYS & set(payload)):
        return "Error"
    return None


def _classify_listed(
    worker: str,
    servers: frozenset[str],
    tools: tuple[str, ...],
    found: set[tuple[str, str]],
    call_tool: CallTool,
    timeout_s: float,
) -> dict[str, Any]:
    missing = sorted(tool for tool in tools if not _is_listed(tool, found))
    if missing:
        return _row(worker, servers, "missing", "unlisted: " + ",".join(missing))
    reads = sorted(tool for tool in tools if tool not in _MUTATING_TOOLS)
    if not reads:
        return _row(worker, servers, "reachable", "listed")
    skipped: list[str] = []
    for tool in reads:
        try:
            payload = _call_with_timeout(partial(call_tool, tool), timeout_s)
        except TimeoutError:
            return _row(worker, servers, "failed", f"probe timed out: {tool}", "TimeoutError")
        except Exception as exc:
            return _row(
                worker, servers, "failed",
                f"{type(exc).__name__}: {exc}",
                type(exc).__name__,
            )
        if isinstance(payload, dict) and payload.get("skipped") is True:
            skipped.append(tool)
            continue
        failure = _probe_is_failure(payload)
        if failure:
            detail = ""
            if isinstance(payload, dict) and payload.get("error"):
                detail = str(payload.get("error"))
            return _row(worker, servers, "failed", detail or failure, failure)
    if skipped:
        return _row(worker, servers, "missing", "not_provided: " + ",".join(skipped))
    return _row(worker, servers, "reachable", "listed")


def _stub_report(workers: dict[str, frozenset[str]], reason: str) -> dict[str, Any]:
    rows = [
        _row(name, servers, "stubbed", reason)
        for name, servers in sorted(workers.items())
    ]
    return _envelope(rows, mode="stub")


def _envelope(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    return {
        "gateway_mode": mode,
        "plain_mcp": bool(plain_mcp_enabled()),
        "workers": rows,
    }


def _failed_dependents(
    pending: list[tuple[str, frozenset[str], tuple[str, ...]]],
    exc: BaseException,
) -> list[dict[str, Any]]:
    reason = f"{type(exc).__name__}: {exc}"
    error_class = type(exc).__name__
    return [
        _row(name, servers, "failed", reason, error_class)
        for name, servers, _tools in pending
    ]


def diagnose(
    *,
    workers: dict[str, frozenset[str]] | None = None,
    tools_for: dict[str, tuple[str, ...]] | None = None,
    list_tools: ListTools | None = None,
    call_tool: CallTool | None = None,
    timeout_s: float | None = None,
    configured: bool | None = None,
    assume_endpoint: bool | None = None,
) -> dict[str, Any]:
    """Build the connection report. Inject list/call callables in tests.

    ``assume_endpoint`` overrides URL/ARN detection. Tests that inject
    ``list_tools`` pass True so classification uses the injected catalog.
    """
    table = workers if workers is not None else worker_servers()
    if configured is None:
        configured = (not force_stub_gateway()) and bool(resolved_gateway_url() or _has_any_arn())
    if not configured:
        reason = "GATEWAY_MODE=stub" if force_stub_gateway() else "nothing_configured"
        return _stub_report(table, reason)

    timeout = timeout_s if timeout_s is not None else mcp_call_timeout_seconds()
    rows: list[dict[str, Any]] = []
    pending: list[tuple[str, frozenset[str], tuple[str, ...]]] = []
    for name, servers in sorted(table.items()):
        tools = _tools_for(name, servers, tools_for)
        if not tools:
            rows.append(_row(name, servers, "reachable", "no_external_tools"))
            continue
        if servers and not all(_has_endpoint(server, assume_endpoint) for server in servers):
            rows.append(_row(name, servers, "missing", "no endpoint configured"))
            continue
        pending.append((name, servers, tools))

    if not pending:
        rows.sort(key=lambda row: row["worker"])
        return _envelope(rows, mode="live")

    lister, caller = _resolve_probes(list_tools, call_tool)
    try:
        names = _call_with_timeout(lister, timeout)
    except Exception as exc:
        rows.extend(_failed_dependents(pending, exc))
        rows.sort(key=lambda row: row["worker"])
        return _envelope(rows, mode="live")

    found = _listed_identities(list(names or []))
    for name, servers, tools in pending:
        rows.append(_classify_listed(name, servers, tools, found, caller, timeout))
    rows.sort(key=lambda row: row["worker"])
    return _envelope(rows, mode="live")


def report_exit_code(report: dict[str, Any]) -> int:
    """2 when any required worker is missing or failed, else 0."""
    for row in report.get("workers") or []:
        if row.get("required") and row.get("state") in {"missing", "failed"}:
            return 2
    return 0


def format_text(report: dict[str, Any]) -> str:
    """Stable human-readable report. No timestamps, no secrets."""
    lines = [
        f"gateway_mode={report.get('gateway_mode', '')} plain_mcp={str(bool(report.get('plain_mcp'))).lower()}",
    ]
    for row in report.get("workers") or []:
        error_class = row.get("error_class") or "-"
        lines.append(
            f"{row['worker']}\t{row['state']}\trequired={str(bool(row['required'])).lower()}"
            f"\terror_class={error_class}\treason={row.get('reason', '')}"
        )
    return "\n".join(lines) + "\n"


def format_json(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def _default_gateway() -> McpGateway:
    """A non-singleton gateway so diagnostics do not mutate investigation state."""
    return McpGateway()


def _resolve_probes(
    list_tools: ListTools | None,
    call_tool: CallTool | None,
) -> tuple[ListTools, CallTool]:
    """Share one gateway when the caller did not inject both probes."""
    if list_tools is not None and call_tool is not None:
        return list_tools, call_tool
    gateway = _default_gateway()

    def lister() -> list[str]:
        return _list_with_retry(gateway)

    def caller(tool: str) -> Any:
        return _call_with_retry(gateway, tool)

    return list_tools or lister, call_tool or caller


def _list_with_retry(gateway: McpGateway) -> list[str]:
    def _once() -> list[str]:
        client = gateway._get_mcp_client()
        if client is None:
            raise RuntimeError("MCP client could not be built")
        listed = _call_with_timeout(client.list_tools_sync, mcp_call_timeout_seconds())
        return tool_names_from_list(listed)

    return _retry_401(gateway, _once)


def _call_with_retry(gateway: McpGateway, tool: str) -> dict[str, Any]:
    def _once() -> dict[str, Any]:
        client = gateway._get_mcp_client()
        if client is None:
            raise RuntimeError("MCP client could not be built")
        timeout_s = mcp_call_timeout_seconds()
        raw = _call_with_timeout(
            lambda: client.call_tool_sync(
                tool_use_id="sentinalai-diag",
                name=outbound_tool_name(tool),
                arguments={},
                read_timeout_seconds=timedelta(seconds=timeout_s),
            ),
            timeout_s,
        )
        return normalize_mcp_result(raw, tool)

    return _retry_401(gateway, _once)


def _retry_401(gateway: McpGateway, fn: Callable[[], Any]) -> Any:
    """One retry after a 401, then the second failure propagates."""
    try:
        return fn()
    except Exception as exc:
        if not _is_unauthorized(exc) or gateway._oauth2_provider is None:
            raise
        gateway._oauth2_provider.invalidate()
        gateway._mcp_client = None
        return fn()


def _strip_nondeterministic(value: Any) -> Any:
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key in sorted(value):
            lowered = str(key).lower()
            if lowered in _DROP_KEYS or any(part in lowered for part in _DROP_KEY_FRAGMENTS):
                continue
            kept[str(key)] = _strip_nondeterministic(value[key])
        return kept
    if isinstance(value, list):
        return [_strip_nondeterministic(item) for item in value]
    return value


# Contract v1 asked for these public names. They are not on the investigate()
# result. v1.1 replaces them with fields that do exist:
#   incident_type          → rca_report.incident_type
#   hypothesis_ranking     → rca_report.winner_hypothesis + rca_report.hypothesis_count
#   tool_call_sequence     → receipts (sequence_order, tool, action, params, status, error, missing_reason)
REPLACED_REPLAY_FIELDS: tuple[str, ...] = (
    "incident_type",
    "hypothesis_ranking",
    "tool_call_sequence",
)

_RECEIPT_FIELDS = (
    "sequence_order", "tool", "action", "params", "status", "error", "missing_reason",
)


def _receipt_sort_key(row: Any) -> tuple[Any, ...]:
    if not isinstance(row, dict):
        return (10**9, "", "")
    order = row.get("sequence_order")
    if not isinstance(order, int):
        order = 10**9
    return (order, str(row.get("tool") or ""), str(row.get("action") or ""))


def replay_canonical(
    result: dict[str, Any],
    connection_report: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Canonical replay document plus the names of fields that are absent.

    Absent fields are reported. Nothing is invented in their place.
    ``incident_type`` (top-level), ``hypothesis_ranking``, and
    ``tool_call_sequence`` are not read; see ``REPLACED_REPLAY_FIELDS``.
    """
    missing: list[str] = []
    document: dict[str, Any] = {}
    for field in ("incident_id", "root_cause", "confidence"):
        if field in result:
            document[field] = result[field]
        else:
            missing.append(field)
    if "evidence_timeline" in result:
        document["evidence_timeline"] = _strip_nondeterministic(result["evidence_timeline"])
    else:
        missing.append("evidence_timeline")

    report = result.get("rca_report")
    if not isinstance(report, dict):
        missing.append("rca_report")
    else:
        rca: dict[str, Any] = {}
        for key in ("incident_type", "winner_hypothesis", "hypothesis_count"):
            if key in report:
                rca[key] = report[key]
            else:
                missing.append(f"rca_report.{key}")
        document["rca_report"] = rca

    receipts = result.get("receipts")
    if isinstance(receipts, list):
        projected: list[Any] = []
        for item in receipts:
            if not isinstance(item, dict):
                projected.append(item)
                continue
            row = {key: item[key] for key in _RECEIPT_FIELDS if key in item}
            projected.append(_strip_nondeterministic(row))
        # sequence_order is the call order. The returned list is not always
        # in that order (a historical-context future races the playbook), so
        # the canonical list follows sequence_order.
        projected.sort(key=_receipt_sort_key)
        document["receipts"] = projected
    else:
        missing.append("receipts")

    for field in (
        "_evidence_lifecycle",
        "citations",
        "validated_claims",
        "non_validated_claims",
        "_corpus_version",
    ):
        if field not in result:
            missing.append(field)
            continue
        if field == "_corpus_version":
            document[field] = result[field]
        else:
            document[field] = _strip_nondeterministic(result[field])

    if connection_report is not None:
        document["connection_report"] = _strip_nondeterministic(connection_report)
    return document, missing


def canonical_json(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def replay_hash(
    result: dict[str, Any],
    connection_report: dict[str, Any] | None = None,
) -> str:
    document, _missing = replay_canonical(result, connection_report)
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
