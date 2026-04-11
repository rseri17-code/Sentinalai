"""Tests for tool auto-discovery (McpGateway.discover_tools).

Covers:
  - discover_tools() returns all known servers when no URL configured
  - discover_tools() parses standard {"available_servers": [...]} format
  - discover_tools() parses stub {"stub_tools": {...}} format
  - discover_tools() parses gateway {"tools": [...]} format
  - discover_tools() caches results within TTL
  - discover_tools() force_refresh bypasses cache
  - discover_tools() falls back gracefully on network error
  - SentinalAISupervisor only instantiates workers for available tools
  - Worker skipped when required server not available
  - knowledge_worker always instantiated (no server dependency)
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from workers.mcp_client import McpGateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_gateway() -> McpGateway:
    """Return a new McpGateway with clean state (no cache)."""
    gw = McpGateway()
    gw._tools_cache = None
    return gw


# ---------------------------------------------------------------------------
# _parse_discovery_response
# ---------------------------------------------------------------------------

class TestParseDiscoveryResponse:
    def test_standard_format(self):
        data = {"available_servers": ["splunk", "github", "sysdig"]}
        result = McpGateway._parse_discovery_response(data)
        assert "splunk" in result
        assert "github" in result
        assert "sysdig" in result

    def test_stub_catalog_format(self):
        data = {"stub_tools": {"splunk": ["search_logs"], "servicenow": ["get_ci_details"]}}
        result = McpGateway._parse_discovery_response(data)
        assert "splunk" in result
        assert "servicenow" in result

    def test_gateway_list_format_with_server_field(self):
        data = {"tools": [{"server": "splunk", "name": "search"}, {"server": "github"}]}
        result = McpGateway._parse_discovery_response(data)
        assert "splunk" in result
        assert "github" in result

    def test_empty_tools_list_returns_all_known(self):
        data = {"tools": []}
        result = McpGateway._parse_discovery_response(data)
        assert len(result) >= 8  # falls back to all known

    def test_unknown_format_returns_all_known(self):
        data = {"something_else": True}
        result = McpGateway._parse_discovery_response(data)
        assert "splunk" in result
        assert "servicenow" in result

    def test_lowercases_server_names(self):
        data = {"available_servers": ["SPLUNK", "GitHub"]}
        result = McpGateway._parse_discovery_response(data)
        assert "splunk" in result
        assert "github" in result


# ---------------------------------------------------------------------------
# discover_tools — no gateway configured
# ---------------------------------------------------------------------------

class TestDiscoverToolsNoGateway:
    def test_returns_all_known_servers_when_no_url(self):
        gw = _fresh_gateway()
        with patch.dict("os.environ", {"AGENTCORE_GATEWAY_URL": "", "STUB_TOOLS_URL": "",
                                       "TOOL_DISCOVERY_URL": ""}):
            result = gw.discover_tools()
        assert "splunk" in result
        assert "servicenow" in result
        assert len(result) >= 8

    def test_result_is_frozenset(self):
        gw = _fresh_gateway()
        with patch.dict("os.environ", {"AGENTCORE_GATEWAY_URL": "", "STUB_TOOLS_URL": "",
                                       "TOOL_DISCOVERY_URL": ""}):
            result = gw.discover_tools()
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# discover_tools — HTTP fetch
# ---------------------------------------------------------------------------

class TestDiscoverToolsHttp:
    def test_uses_tool_discovery_url_env_var(self):
        gw = _fresh_gateway()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"available_servers": ["splunk", "servicenow"]}
        mock_resp.raise_for_status = MagicMock()

        with patch("workers.mcp_client._requests_lib") as mock_req:
            mock_req.get.return_value = mock_resp
            with patch.dict("os.environ", {"TOOL_DISCOVERY_URL": "http://gateway/tools"}):
                result = gw.discover_tools(force_refresh=True)

        mock_req.get.assert_called_once_with("http://gateway/tools", timeout=5)
        assert "splunk" in result
        assert "servicenow" in result

    def test_falls_back_on_http_error(self):
        gw = _fresh_gateway()
        with patch("workers.mcp_client._requests_lib") as mock_req:
            mock_req.get.side_effect = ConnectionError("refused")
            with patch.dict("os.environ", {"TOOL_DISCOVERY_URL": "http://bad-host/tools"}):
                result = gw.discover_tools(force_refresh=True)

        # Graceful fallback — all known servers
        assert "splunk" in result
        assert len(result) >= 8

    def test_falls_back_on_bad_json(self):
        gw = _fresh_gateway()
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("bad json")
        mock_resp.raise_for_status = MagicMock()

        with patch("workers.mcp_client._requests_lib") as mock_req:
            mock_req.get.return_value = mock_resp
            with patch.dict("os.environ", {"TOOL_DISCOVERY_URL": "http://gw/tools"}):
                result = gw.discover_tools(force_refresh=True)

        assert "splunk" in result


# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------

class TestDiscoverToolsCache:
    def test_caches_result(self):
        gw = _fresh_gateway()
        first_result = frozenset({"splunk", "servicenow"})
        gw._tools_cache = (time.monotonic(), first_result)

        # Even with a mock URL, it should return cached value
        with patch("workers.mcp_client._requests_lib") as mock_req:
            result = gw.discover_tools()

        mock_req.get.assert_not_called()  # never hit network
        assert result is first_result

    def test_force_refresh_bypasses_cache(self):
        gw = _fresh_gateway()
        cached = frozenset({"splunk"})
        gw._tools_cache = (time.monotonic(), cached)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"available_servers": ["splunk", "github"]}
        mock_resp.raise_for_status = MagicMock()

        with patch("workers.mcp_client._requests_lib") as mock_req:
            mock_req.get.return_value = mock_resp
            with patch.dict("os.environ", {"TOOL_DISCOVERY_URL": "http://gw/tools"}):
                result = gw.discover_tools(force_refresh=True)

        assert "github" in result  # fresh result

    def test_stale_cache_triggers_refetch(self):
        gw = _fresh_gateway()
        stale_time = time.monotonic() - (McpGateway._DISCOVERY_TTL_SECONDS + 10)
        gw._tools_cache = (stale_time, frozenset({"old"}))

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"available_servers": ["splunk"]}
        mock_resp.raise_for_status = MagicMock()

        with patch("workers.mcp_client._requests_lib") as mock_req:
            mock_req.get.return_value = mock_resp
            with patch.dict("os.environ", {"TOOL_DISCOVERY_URL": "http://gw/tools"}):
                result = gw.discover_tools()

        # Should have re-fetched (stale cache)
        mock_req.get.assert_called_once()


# ---------------------------------------------------------------------------
# SentinalAISupervisor worker filtering
# ---------------------------------------------------------------------------

class TestSupervisorWorkerFiltering:
    def _make_supervisor_with_servers(self, servers: set[str]):
        """Create a supervisor using a mock gateway that reports given servers."""
        mock_gw = MagicMock()
        mock_gw.discover_tools.return_value = frozenset(servers)

        with patch("supervisor.agent.OpsWorker"), \
             patch("supervisor.agent.LogWorker"), \
             patch("supervisor.agent.MetricsWorker"), \
             patch("supervisor.agent.ApmWorker"), \
             patch("supervisor.agent.KnowledgeWorker"), \
             patch("supervisor.agent.ItsmWorker"), \
             patch("supervisor.agent.DevopsWorker"), \
             patch("supervisor.agent.ConfluenceWorker"), \
             patch("supervisor.agent.CodeWorker"):
            from supervisor.agent import SentinalAISupervisor
            sup = SentinalAISupervisor(gateway=mock_gw)
        return sup

    def test_all_workers_present_when_all_servers_available(self):
        sup = self._make_supervisor_with_servers({
            "moogsoft", "splunk", "sysdig", "dynatrace", "servicenow",
            "github", "confluence",
        })
        assert "ops_worker" in sup.workers
        assert "log_worker" in sup.workers
        assert "itsm_worker" in sup.workers
        assert "knowledge_worker" in sup.workers

    def test_knowledge_worker_always_present(self):
        # Even with no external tools available
        sup = self._make_supervisor_with_servers(set())
        assert "knowledge_worker" in sup.workers

    def test_ops_worker_skipped_without_moogsoft(self):
        sup = self._make_supervisor_with_servers({"splunk", "servicenow"})
        assert "ops_worker" not in sup.workers

    def test_log_worker_skipped_without_splunk(self):
        sup = self._make_supervisor_with_servers({"moogsoft", "servicenow"})
        assert "log_worker" not in sup.workers

    def test_apm_worker_present_with_dynatrace(self):
        sup = self._make_supervisor_with_servers({"dynatrace"})
        assert "apm_worker" in sup.workers

    def test_apm_worker_present_with_signalfx(self):
        sup = self._make_supervisor_with_servers({"signalfx"})
        assert "apm_worker" in sup.workers

    def test_apm_worker_skipped_without_either_apm_tool(self):
        sup = self._make_supervisor_with_servers({"splunk", "servicenow"})
        assert "apm_worker" not in sup.workers

    def test_confluence_worker_skipped_without_confluence(self):
        sup = self._make_supervisor_with_servers({"splunk", "github"})
        assert "confluence_worker" not in sup.workers

    def test_code_worker_skipped_without_github(self):
        sup = self._make_supervisor_with_servers({"splunk", "servicenow"})
        assert "code_worker" not in sup.workers

    def test_devops_worker_and_code_worker_both_present_with_github(self):
        sup = self._make_supervisor_with_servers({"github"})
        assert "devops_worker" in sup.workers
        assert "code_worker" in sup.workers
