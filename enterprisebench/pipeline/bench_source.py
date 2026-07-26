"""BenchMCPSource — a deterministic, production-parity MCP gateway for one scenario.

This is the injection boundary of the Investigation Evaluation Pipeline. It is a
drop-in for ``workers.mcp_client.McpGateway`` (duck-typed: ``invoke`` +
``discover_tools``), passed to ``SentinalAISupervisor(gateway=...)`` so the
UNMODIFIED engine issues its real MCP queries against rendered EFIC telemetry.

Production parity:

* Responds ONLY to queries the engine issues (pull, not push); it never volunteers
  evidence the engine did not ask for.
* Respects the query's server + action; unmatched queries fall through to the real
  ``workers.mcp_client`` stub dispatch, so absent evidence returns the exact
  production-shaped empty response (``{"logs": {"results": [], ...}}`` etc.) — a
  real "no data" answer, not an error and not a hidden hint.
* Deterministic: the rendered channels are fixed, iteration is stable, no clock,
  no randomness. Repeated runs issue identical responses.

Isolation invariant (proven in tests): the source is constructed from a
:class:`~enterprisebench.pipeline.render.RenderedScenario`, which is derived from
the PUBLIC ``task.incident`` + ``task.telemetry`` only. It never holds — and can
never return — ``ground_truth``, ``traps``, the reasoning ``efic`` block, or the
hidden ``investigation_spec``.
"""
from __future__ import annotations

from typing import Any, Mapping

from enterprisebench.pipeline.render import RenderedScenario

# Servers advertised to the engine's tool discovery so every collection worker
# registers (mirrors the real gateway's _ALL_KNOWN_SERVERS fallback).
_ADVERTISED_SERVERS = frozenset({
    "moogsoft", "splunk", "sysdig", "dynatrace", "signalfx",
    "servicenow", "github", "confluence", "kubernetes",
})


def _route(server: str, action: str) -> str | None:
    """Map an engine (server, action) query to a rendered-channel key, or None
    to fall through to the production stub empty."""
    a = action.lower()
    if server == "moogsoft":
        return "moogsoft.get_incident_by_id"
    if server == "splunk":
        return "splunk.get_change_data" if "change" in a else "splunk.search_logs"
    if server == "dynatrace":
        if "golden" in a or "signal" in a:
            return "dynatrace.get_golden_signals"
        return None
    if server == "sysdig":
        return "sysdig.get_events" if "event" in a else "sysdig.query_metrics"
    if server == "servicenow":
        if "change" in a:
            return "servicenow.get_change_records"
        if "ci" in a:
            return "servicenow.get_ci_details"
        return None
    if server == "route53":                       # IE-2 (flag-gated at render)
        return "route53.check_resolver" if "resolver" in a else "route53.get_record"
    return None


class BenchMCPSource:
    """A per-scenario deterministic gateway. Duck-types ``McpGateway``."""

    def __init__(self, rendered: RenderedScenario) -> None:
        self._channels = rendered.channels
        self.provenance = rendered.provenance
        # Observable request trace (the raw MCP interaction stream).
        self.query_log: list[dict[str, Any]] = []

    # -- McpGateway interface -------------------------------------------------
    def discover_tools(self) -> frozenset[str]:
        # IE-2: advertise route53 only when the pilot flag is on, so the engine
        # registers the DNS worker only in that mode (flag-off is unchanged).
        import os
        if os.environ.get("IE_DNS_ENABLED", "false").lower() in ("1", "true", "yes"):
            return _ADVERTISED_SERVERS | {"route53"}
        return _ADVERTISED_SERVERS

    def invoke(self, mcp_tool_name: str, tool_action: str,
               params: Mapping[str, Any] | None = None,
               user_identity: str | None = None) -> dict[str, Any]:
        params = params or {}
        server = str(mcp_tool_name).split(".", 1)[0]
        key = _route(server, str(tool_action))
        matched = key is not None and key in self._channels
        if matched:
            response = self._channels[key]
        else:
            response = _stub_empty(mcp_tool_name, tool_action, params)

        self.query_log.append({
            "seq": len(self.query_log),
            "server": server,
            "mcp_tool_name": str(mcp_tool_name),
            "action": str(tool_action),
            "query": str(params.get("query", "")),
            "matched_channel": key if matched else None,
            "served": "rendered" if matched else "empty",
        })
        return response

    # -- introspection (evaluation-only) --------------------------------------
    def servers_queried(self) -> list[str]:
        seen: list[str] = []
        for q in self.query_log:
            if q["server"] not in seen:
                seen.append(q["server"])
        return seen

    def rendered_channels_served(self) -> list[str]:
        return sorted({q["matched_channel"] for q in self.query_log
                       if q["matched_channel"]})


def _stub_empty(mcp_tool_name: str, tool_action: str,
                params: Mapping[str, Any]) -> dict[str, Any]:
    """Delegate to the real stub dispatch for a production-shaped empty response.

    Reused (not re-implemented) so "no data" looks exactly like production. Import
    is lazy to avoid importing the worker layer at module load."""
    from workers.mcp_client import _stub_response
    return _stub_response(mcp_tool_name, tool_action, dict(params))


__all__ = ["BenchMCPSource"]
