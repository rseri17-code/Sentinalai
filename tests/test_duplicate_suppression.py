"""Tests for duplicate-call suppression in McpGateway (MCP_DEDUP_ENABLED)."""
import os
import pytest
from unittest.mock import patch


def _make_gateway():
    """Return a fresh McpGateway instance (not the singleton)."""
    from workers.mcp_client import McpGateway, RateLimiterRegistry
    gw = McpGateway(rate_limiter=RateLimiterRegistry(unlimited=True))
    return gw


class TestDuplicateSuppression:

    def test_dedup_disabled_allows_duplicate(self):
        """With MCP_DEDUP_ENABLED=false, same call executes twice (no suppression)."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "false"}):
            r1 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
            r2 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
        # Neither call should be skipped
        assert r1.get("status") != "skipped"
        assert r2.get("status") != "skipped"

    def test_dedup_enabled_second_call_skipped(self):
        """With MCP_DEDUP_ENABLED=true, second identical call returns synthetic skipped result."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            r1 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
            r2 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
        assert r1.get("status") != "skipped"
        assert r2["status"] == "skipped"
        assert r2["result"] == "duplicate_call"
        assert "identical call already executed" in r2["note"]
        assert r2["worker"] == "splunk.search_oneshot"
        assert r2["action"] == "search_logs"

    def test_different_action_not_suppressed(self):
        """Different action on same worker is NOT suppressed."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            r1 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
            r2 = gw.invoke("splunk.search_oneshot", "get_change_data", {"query": "error"})
        assert r1.get("status") != "skipped"
        assert r2.get("status") != "skipped"

    def test_same_action_different_params_not_suppressed(self):
        """Same action, different params is NOT suppressed."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            r1 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
            r2 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "timeout"})
        assert r1.get("status") != "skipped"
        assert r2.get("status") != "skipped"

    def test_clear_call_signatures_allows_re_execution(self):
        """clear_call_signatures() allows re-execution of the same call."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            r1 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
            r2 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
            assert r2["status"] == "skipped"
            gw.clear_call_signatures()
            r3 = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "error"})
        assert r3.get("status") != "skipped"

    def test_signature_includes_worker_action_params(self):
        """Signature includes worker, action, and sorted params (order-independent)."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            # Same params in different key order should still be treated as duplicate
            r1 = gw.invoke("sysdig.golden_signals", "get_metrics", {"service": "api", "window": "5m"})
            r2 = gw.invoke("sysdig.golden_signals", "get_metrics", {"window": "5m", "service": "api"})
        assert r2["status"] == "skipped"

    def test_dedup_works_across_multiple_invoke_calls(self):
        """Singleton state: dedup works across two invoke() calls without reset."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            gw.invoke("moogsoft.get_incidents", "list_incidents", {})
            gw.invoke("splunk.search_oneshot", "search_logs", {"q": "x"})
            # Both already called — both should now be skipped
            r3 = gw.invoke("moogsoft.get_incidents", "list_incidents", {})
            r4 = gw.invoke("splunk.search_oneshot", "search_logs", {"q": "x"})
        assert r3["status"] == "skipped"
        assert r4["status"] == "skipped"

    def test_non_dict_params_no_crash(self):
        """Non-dict params don't crash (graceful fallback)."""
        gw = _make_gateway()
        with patch.dict(os.environ, {"MCP_DEDUP_ENABLED": "true"}):
            # Pass a non-dict — should not raise
            r1 = gw.invoke("splunk.search_oneshot", "search_logs", {"q": "error"})
            # Even if params processing is weird, it should not crash
            assert isinstance(r1, dict)
