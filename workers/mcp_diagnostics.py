"""Connection diagnostics for each worker's MCP server.

One row per tool, plus one rollup per worker. Tool rows are sorted by
worker name, then tool name. A worker's state is the worst state among
its required tools (failed > missing > stubbed > reachable). Optional
tools are reported and do not move the worker state.

- reachable: the endpoint answered within the probe timeout and the tool
  is listed, and a read-only probe is not an honest skip.
- stubbed: fixtures, because GATEWAY_MODE=stub or nothing is configured.
- missing: no endpoint, or the endpoint is up but does not provide this
  tool (unlisted, or listed only as ``skipped: true``).
- failed: the probe errored, timed out, was rejected after one 401 retry,
  or the MCP client could not be built.

Which tools are required does not change in this report. Every tool in
``WORKER_TOOLS`` is required. ``splunk.get_change_data`` stays required,
so a skip there keeps ``log_worker`` missing even when
``splunk.search_oneshot`` is reachable.

A tool the OSS validation shim advertises but answers with ``skipped: true``
is not provided. That is missing, not stubbed and not reachable. The client
does not reimplement the shim; it only reads the payload the shim already
returns.

The report is deterministic for the same inputs: workers and tools are
sorted, and the report has no timestamps. Secrets are scrubbed.
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


# failed > missing > stubbed > reachable. Optional tools are not included.
STATE_RANK: dict[str, int] = {
    "reachable": 0,
    "stubbed": 1,
    "missing": 2,
    "failed": 3,
}


def worst_state(states: list[str]) -> str:
    """Worst connection state among required tools. Empty is reachable."""
    if not states:
        return "reachable"
    return max(states, key=lambda state: STATE_RANK.get(state, 0))


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


def _tool_row(
    worker: str,
    tool: str,
    required: bool,
    state: str,
    detail: str,
    error_class: str | None = None,
) -> dict[str, Any]:
    """Public tool fields plus a private error class for the worker rollup."""
    return {
        "worker": worker,
        "tool": tool,
        "required": required,
        "state": state,
        "detail": scrub_secrets(detail),
        "_error_class": error_class,
    }


def _public_tool(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "_error_class"}


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


def _is_required_tool(tool: str, optional: frozenset[str]) -> bool:
    return tool not in optional


def _classify_tool(
    tool: str,
    found: set[tuple[str, str]],
    call_tool: CallTool,
    timeout_s: float,
) -> tuple[str, str, str | None]:
    """Return state, detail, error_class for one tool."""
    if not _is_listed(tool, found):
        return "missing", "unlisted", None
    if tool in _MUTATING_TOOLS:
        return "reachable", "listed", None
    try:
        payload = _call_with_timeout(partial(call_tool, tool), timeout_s)
    except TimeoutError:
        return "failed", f"probe timed out: {tool}", "TimeoutError"
    except Exception as exc:
        return "failed", f"{type(exc).__name__}: {exc}", type(exc).__name__
    if isinstance(payload, dict) and payload.get("skipped") is True:
        return "missing", "not_provided", None
    failure = _probe_is_failure(payload)
    if failure:
        detail = ""
        if isinstance(payload, dict) and payload.get("error"):
            detail = str(payload.get("error"))
        return "failed", detail or failure, failure
    return "reachable", "listed", None


def _worker_reason(tool_rows: list[dict[str, Any]], state: str) -> tuple[str, str | None]:
    """Worker reason string compatible with the v1 report, plus error class."""
    required = [row for row in tool_rows if row["required"]]
    if state == "reachable":
        return "listed", None
    matching = [row for row in required if row["state"] == state]
    if state == "missing":
        unlisted = sorted(row["tool"] for row in matching if row["detail"] == "unlisted")
        skipped = sorted(row["tool"] for row in matching if row["detail"] == "not_provided")
        parts: list[str] = []
        if unlisted:
            parts.append("unlisted: " + ",".join(unlisted))
        if skipped:
            parts.append("not_provided: " + ",".join(skipped))
        other = sorted(
            row["tool"] for row in matching
            if row["detail"] not in {"unlisted", "not_provided"}
        )
        if other and not parts:
            parts.append(matching[0]["detail"])
        elif other:
            parts.append("missing: " + ",".join(other))
        return ("; ".join(parts) or "missing"), None
    if state == "failed":
        row = sorted(matching, key=lambda item: item["tool"])[0]
        return row["detail"], row.get("_error_class")
    if state == "stubbed":
        return (matching[0]["detail"] if matching else "stubbed"), None
    return state, None


def _rollup(
    worker: str,
    servers: frozenset[str],
    tool_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Worker state is the worst required tool. Optional tools stay on the side."""
    required_states = [row["state"] for row in tool_rows if row["required"]]
    if not required_states:
        return _row(worker, servers, "reachable", "listed")
    state = worst_state(required_states)
    reason, error_class = _worker_reason(tool_rows, state)
    return _row(worker, servers, state, reason, error_class)


def _classify_listed(
    worker: str,
    servers: frozenset[str],
    tools: tuple[str, ...],
    found: set[tuple[str, str]],
    call_tool: CallTool,
    timeout_s: float,
    optional: frozenset[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tool_rows: list[dict[str, Any]] = []
    for tool in sorted(tools):
        state, detail, error_class = _classify_tool(tool, found, call_tool, timeout_s)
        tool_rows.append(_tool_row(
            worker, tool, _is_required_tool(tool, optional), state, detail, error_class,
        ))
    return _rollup(worker, servers, tool_rows), tool_rows


def _stub_report(
    workers: dict[str, frozenset[str]],
    reason: str,
    tools_for: dict[str, tuple[str, ...]] | None,
    optional: frozenset[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    tool_rows: list[dict[str, Any]] = []
    for name, servers in sorted(workers.items()):
        rows.append(_row(name, servers, "stubbed", reason))
        for tool in sorted(_tools_for(name, servers, tools_for)):
            tool_rows.append(_tool_row(
                name, tool, _is_required_tool(tool, optional), "stubbed", reason,
            ))
    return _envelope(rows, tool_rows, mode="stub")


def _envelope(
    rows: list[dict[str, Any]],
    tool_rows: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["worker"])
    public_tools = [_public_tool(row) for row in tool_rows]
    public_tools.sort(key=lambda row: (row["worker"], row["tool"]))
    return {
        "gateway_mode": mode,
        "plain_mcp": bool(plain_mcp_enabled()),
        "workers": rows,
        "tools": public_tools,
    }


def _failed_dependents(
    pending: list[tuple[str, frozenset[str], tuple[str, ...]]],
    exc: BaseException,
    optional: frozenset[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reason = f"{type(exc).__name__}: {exc}"
    error_class = type(exc).__name__
    rows: list[dict[str, Any]] = []
    tool_rows: list[dict[str, Any]] = []
    for name, servers, tools in pending:
        rows.append(_row(name, servers, "failed", reason, error_class))
        for tool in sorted(tools):
            tool_rows.append(_tool_row(
                name, tool, _is_required_tool(tool, optional), "failed", reason, error_class,
            ))
    return rows, tool_rows


def diagnose(
    *,
    workers: dict[str, frozenset[str]] | None = None,
    tools_for: dict[str, tuple[str, ...]] | None = None,
    list_tools: ListTools | None = None,
    call_tool: CallTool | None = None,
    timeout_s: float | None = None,
    configured: bool | None = None,
    assume_endpoint: bool | None = None,
    optional_tools: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build the connection report. Inject list/call callables in tests.

    ``assume_endpoint`` overrides URL/ARN detection. Tests that inject
    ``list_tools`` pass True so classification uses the injected catalog.

    ``optional_tools`` defaults to empty. Every ``WORKER_TOOLS`` entry stays
    required unless a test names it here. Optional tools are still probed.
    """
    table = workers if workers is not None else worker_servers()
    optional = optional_tools or frozenset()
    if configured is None:
        configured = (not force_stub_gateway()) and bool(resolved_gateway_url() or _has_any_arn())
    if not configured:
        reason = "GATEWAY_MODE=stub" if force_stub_gateway() else "nothing_configured"
        return _stub_report(table, reason, tools_for, optional)

    timeout = timeout_s if timeout_s is not None else mcp_call_timeout_seconds()
    rows: list[dict[str, Any]] = []
    tool_rows: list[dict[str, Any]] = []
    pending: list[tuple[str, frozenset[str], tuple[str, ...]]] = []
    for name, servers in sorted(table.items()):
        tools = _tools_for(name, servers, tools_for)
        if not tools:
            rows.append(_row(name, servers, "reachable", "no_external_tools"))
            continue
        if servers and not all(_has_endpoint(server, assume_endpoint) for server in servers):
            rows.append(_row(name, servers, "missing", "no endpoint configured"))
            for tool in sorted(tools):
                tool_rows.append(_tool_row(
                    name, tool, _is_required_tool(tool, optional),
                    "missing", "no endpoint configured",
                ))
            continue
        pending.append((name, servers, tools))

    if not pending:
        return _envelope(rows, tool_rows, mode="live")

    lister, caller = _resolve_probes(list_tools, call_tool)
    try:
        names = _call_with_timeout(lister, timeout)
    except Exception as exc:
        failed_rows, failed_tools = _failed_dependents(pending, exc, optional)
        rows.extend(failed_rows)
        tool_rows.extend(failed_tools)
        return _envelope(rows, tool_rows, mode="live")

    found = _listed_identities(list(names or []))
    for name, servers, tools in pending:
        worker_row, classified = _classify_listed(
            name, servers, tools, found, caller, timeout, optional,
        )
        rows.append(worker_row)
        tool_rows.extend(classified)
    return _envelope(rows, tool_rows, mode="live")


def report_exit_code(report: dict[str, Any]) -> int:
    """2 when any required worker is missing or failed, else 0."""
    for row in report.get("workers") or []:
        if row.get("required") and row.get("state") in {"missing", "failed"}:
            return 2
    return 0


def format_text(report: dict[str, Any]) -> str:
    """Stable human-readable report. Worker line, then its tool rows.

    No timestamps, no secrets.
    """
    lines = [
        f"gateway_mode={report.get('gateway_mode', '')} plain_mcp={str(bool(report.get('plain_mcp'))).lower()}",
    ]
    by_worker: dict[str, list[dict[str, Any]]] = {}
    for tool in report.get("tools") or []:
        by_worker.setdefault(str(tool.get("worker")), []).append(tool)
    for row in report.get("workers") or []:
        error_class = row.get("error_class") or "-"
        lines.append(
            f"{row['worker']}\t{row['state']}\trequired={str(bool(row['required'])).lower()}"
            f"\terror_class={error_class}\treason={row.get('reason', '')}"
        )
        for tool in by_worker.get(row["worker"], []):
            lines.append(
                f"  {tool['tool']}\t{tool['state']}"
                f"\trequired={str(bool(tool['required'])).lower()}"
                f"\tdetail={tool.get('detail', '')}"
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
