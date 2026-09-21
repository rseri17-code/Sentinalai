"""Slice 3 — config / clone UX: GATEWAY_MODE, env template, health, LICENSE note."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import supervisor.llm as llm_module
import workers.mcp_client as mc
from agui.tools_health import build_tools_health
from workers.mcp_client import McpGateway, force_stub_gateway, resolved_gateway_mode


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_gateway():
    McpGateway.reset_instance()
    yield
    McpGateway.reset_instance()


class TestGatewayModeHonored:
    def test_stub_wins_over_configured_url(self):
        gw = McpGateway()
        live = MagicMock(return_value={"live": True})
        gw._invoke_via_gateway = live
        with patch.object(mc, "GATEWAY_MODE", "stub"), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            assert force_stub_gateway() is True
            assert resolved_gateway_mode() == "stub"
            result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        live.assert_not_called()
        assert result == {"logs": {"results": [], "count": 0}}

    def test_live_mode_uses_gateway_when_url_set(self):
        gw = McpGateway()
        live = MagicMock(return_value={"from": "gateway"})
        gw._invoke_via_gateway = live
        with patch.object(mc, "GATEWAY_MODE", "live"), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            assert force_stub_gateway() is False
            assert resolved_gateway_mode() == "live"
            result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        live.assert_called_once()
        assert result == {"from": "gateway"}

    def test_unset_mode_keeps_url_auto_live(self):
        gw = McpGateway()
        live = MagicMock(return_value={"from": "gateway"})
        gw._invoke_via_gateway = live
        with patch.object(mc, "GATEWAY_MODE", ""), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            assert force_stub_gateway() is False
            assert resolved_gateway_mode() == "live"
            result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        live.assert_called_once()
        assert result == {"from": "gateway"}

    def test_live_mode_without_url_is_stub(self):
        with patch.object(mc, "GATEWAY_MODE", "live"), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", ""), \
             patch.object(mc, "MCP_TOOL_ARNS", {k: "" for k in mc.MCP_TOOL_ARNS}):
            assert resolved_gateway_mode() == "stub"


class TestHealthReportsRealPort:
    def test_unused_keys_do_not_mark_llm_configured(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-unused")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-unused")
        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        with patch.object(llm_module, "LLM_ENABLED", False), \
             patch.object(llm_module, "LLM_PROVIDER", "anthropic"), \
             patch.object(mc, "GATEWAY_MODE", "stub"), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            payload = build_tools_health()
        assert payload["llm"]["enabled"] is False
        assert payload["tools"]["llm"]["configured"] is False
        assert payload["tools"]["llm"]["port"] == "NullInference"
        assert payload["gateway_mode"] == "stub"
        assert payload["tools"]["splunk"]["mode"] == "stub"

    def test_enabled_anthropic_reports_port_class(self):
        with patch.object(llm_module, "LLM_ENABLED", True), \
             patch.object(llm_module, "LLM_PROVIDER", "anthropic"), \
             patch.object(llm_module, "MODEL_ID", "claude-sonnet-4-6"), \
             patch.object(llm_module, "_ANTHROPIC_AVAILABLE", True), \
             patch.object(mc, "GATEWAY_MODE", "live"), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            payload = build_tools_health()
        assert payload["llm"]["enabled"] is True
        assert payload["llm"]["provider"] == "anthropic"
        assert payload["llm"]["port"] == "AnthropicInference"
        assert payload["gateway_mode"] == "live"
        assert payload["ready_for_production"] is True


class TestSingleEnvTemplate:
    def test_example_documents_implemented_providers(self):
        text = (ROOT / ".env.example").read_text()
        assert "LLM_ENABLED=false" in text
        assert "LLM_PROVIDER=null" in text
        assert "GATEWAY_MODE=stub" in text
        assert "AGENTCORE_GATEWAY_URL" in text
        assert "NOT implemented" in text
        assert "do not set LLM_PROVIDER=openai" in text
        assert not any(line.strip() == "LLM_PROVIDER=openai" for line in text.splitlines())

    def test_template_is_pointer_not_second_source(self):
        text = (ROOT / ".env.template").read_text()
        assert ".env.example" in text
        assert "LLM_ENABLED=true" not in text
        assert "BEDROCK_MODEL_ID=" not in text.split("\n")[0]


class TestLicenseLeftToOwner:
    def test_no_license_file_invented(self):
        assert not (ROOT / "LICENSE").exists()

    def test_pyproject_stays_proprietary(self):
        text = (ROOT / "pyproject.toml").read_text()
        assert 'license = {text = "Proprietary"}' in text
