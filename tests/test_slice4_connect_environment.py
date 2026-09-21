"""Slice 4 — connect-your-environment: MCP URL alias, YAML playbooks, worker aliases."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import workers.mcp_client as mc
from supervisor.playbook_loader import (
    CANONICAL_WORKERS,
    load_worker_aliases,
    load_yaml_playbooks,
    resolve_worker_alias,
)
from supervisor.sentinel_config import SentinelConfig, get_config, reset_config
from supervisor.tool_selector import INCIDENT_PLAYBOOKS, get_playbook
from workers.mcp_client import McpGateway, resolved_gateway_mode, resolved_gateway_url


ROOT = Path(__file__).resolve().parents[1]


def _write_yaml(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


@pytest.fixture(autouse=True)
def _reset_gateway_and_playbooks():
    McpGateway.reset_instance()
    import supervisor.tool_selector as ts
    ts._get_active_playbooks.__dict__.clear()
    reset_config()
    yield
    McpGateway.reset_instance()
    ts._get_active_playbooks.__dict__.clear()
    reset_config()


# ---------------------------------------------------------------------------
# MCP_GATEWAY_URL alias
# ---------------------------------------------------------------------------

class TestMcpGatewayUrlAlias:
    def test_env_helper_reads_mcp_when_agentcore_unset(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_GATEWAY_URL", raising=False)
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        assert mc._env_gateway_url() == "https://mcp.clone.example"

    def test_env_helper_agentcore_still_works(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://agentcore.example")
        monkeypatch.delenv("MCP_GATEWAY_URL", raising=False)
        assert mc._env_gateway_url() == "https://agentcore.example"

    def test_env_helper_agentcore_wins_when_both_set(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://agentcore.example")
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        assert mc._env_gateway_url() == "https://agentcore.example"

    def test_resolved_url_uses_mcp_alias_when_module_attr_empty(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        with patch.object(mc, "AGENTCORE_GATEWAY_URL", ""):
            assert resolved_gateway_url() == "https://mcp.clone.example"

    def test_resolved_url_patched_agentcore_still_wins(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"):
            assert resolved_gateway_url() == "https://gateway.test"

    def test_mcp_alias_is_live_when_mode_unset(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        gw = McpGateway()
        live = MagicMock(return_value={"from": "gateway"})
        gw._invoke_via_gateway = live
        with patch.object(mc, "GATEWAY_MODE", ""), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", ""), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            assert resolved_gateway_mode() == "live"
            result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        live.assert_called_once()
        assert result == {"from": "gateway"}

    def test_stub_mode_still_wins_with_mcp_alias(self, monkeypatch):
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        gw = McpGateway()
        live = MagicMock(return_value={"from": "gateway"})
        gw._invoke_via_gateway = live
        with patch.object(mc, "GATEWAY_MODE", "stub"), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", ""), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            assert resolved_gateway_mode() == "stub"
            result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        live.assert_not_called()
        assert result == {"logs": {"results": [], "count": 0}}

    def test_agentcore_url_invoke_unchanged(self):
        gw = McpGateway()
        live = MagicMock(return_value={"from": "gateway"})
        gw._invoke_via_gateway = live
        with patch.object(mc, "GATEWAY_MODE", ""), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gateway.test"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            assert resolved_gateway_mode() == "live"
            result = gw.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        live.assert_called_once()
        assert result == {"from": "gateway"}

    def test_config_reads_mcp_alias(self, monkeypatch):
        monkeypatch.delenv("AGENTCORE_GATEWAY_URL", raising=False)
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        reset_config()
        cfg = SentinelConfig.from_env()
        assert cfg.workers.agentcore_gateway_url == "https://mcp.clone.example"

    def test_config_agentcore_wins_over_alias(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://agentcore.example")
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.clone.example")
        reset_config()
        assert get_config().workers.agentcore_gateway_url == "https://agentcore.example"


# ---------------------------------------------------------------------------
# YAML playbooks + worker aliases (flag-gated)
# ---------------------------------------------------------------------------

class TestYamlPlaybooksAndAliases:
    def test_flag_off_returns_hardcoded_identity(self, monkeypatch):
        monkeypatch.delenv("YAML_PLAYBOOKS_ENABLED", raising=False)
        for incident_type, steps in INCIDENT_PLAYBOOKS.items():
            assert get_playbook(incident_type) == INCIDENT_PLAYBOOKS[incident_type]

    def test_flag_off_does_not_rewrite_workers(self, monkeypatch):
        monkeypatch.delenv("YAML_PLAYBOOKS_ENABLED", raising=False)
        workers = {step["worker"] for step in get_playbook("timeout")}
        assert "log_worker" in workers
        assert "splunk" not in workers

    def test_canonical_names_pass_through(self):
        for name in ("log_worker", "apm_worker", "metrics_worker", "itsm_worker"):
            assert resolve_worker_alias(name) == name
            assert name in CANONICAL_WORKERS

    def test_vendor_aliases_resolve(self):
        assert resolve_worker_alias("splunk") == "log_worker"
        assert resolve_worker_alias("prometheus") == "metrics_worker"
        assert resolve_worker_alias("dynatrace") == "apm_worker"
        assert resolve_worker_alias("servicenow") == "itsm_worker"
        assert resolve_worker_alias("moogsoft") == "ops_worker"
        assert resolve_worker_alias("Splunk") == "log_worker"

    def test_unknown_worker_left_unchanged(self):
        assert resolve_worker_alias("custom_worker") == "custom_worker"

    def test_yaml_load_resolves_aliases(self, tmp_path):
        _write_yaml(tmp_path, "timeout.yaml", """
name: timeout
steps:
  - worker: splunk
    action: search_logs
    label: search_timeout_logs
  - worker: prometheus
    action: query_metrics
    label: check_latency_metrics
  - worker: log_worker
    action: get_change_data
    label: check_changes
""")
        result = load_yaml_playbooks(tmp_path)
        workers = [s["worker"] for s in result["timeout"]]
        assert workers == ["log_worker", "metrics_worker", "log_worker"]

    def test_flag_on_loads_yaml_with_aliases(self, monkeypatch, tmp_path):
        _write_yaml(tmp_path, "latency.yaml", """
name: latency
steps:
  - worker: logs
    action: search_logs
    label: search_latency_logs
  - worker: apm
    action: get_golden_signals
    label: check_golden_signals
""")
        monkeypatch.setenv("YAML_PLAYBOOKS_ENABLED", "true")
        monkeypatch.setenv("PLAYBOOKS_DIR", str(tmp_path))
        import supervisor.playbook_loader as pl
        original_dir = pl._PLAYBOOKS_DIR
        pl._PLAYBOOKS_DIR = tmp_path
        try:
            result = get_playbook("latency")
            assert result[0]["worker"] == "log_worker"
            assert result[1]["worker"] == "apm_worker"
            assert result[0]["label"] == "search_latency_logs"
        finally:
            pl._PLAYBOOKS_DIR = original_dir

    def test_alias_overlay_file(self, monkeypatch, tmp_path):
        overlay = tmp_path / "aliases.yaml"
        overlay.write_text("aliases:\n  acme_logs: log_worker\n")
        monkeypatch.setenv("WORKER_ALIASES_PATH", str(overlay))
        aliases = load_worker_aliases()
        assert aliases["acme_logs"] == "log_worker"
        assert aliases["splunk"] == "log_worker"
        assert resolve_worker_alias("acme_logs", aliases) == "log_worker"

    def test_real_playbooks_stay_canonical_with_aliases(self):
        result = load_yaml_playbooks(ROOT / "config" / "playbooks")
        for incident_type, steps in result.items():
            for step in steps:
                assert step["worker"] in CANONICAL_WORKERS, (
                    f"{incident_type} worker {step['worker']!r} is not canonical"
                )


# ---------------------------------------------------------------------------
# Clone docs / env template
# ---------------------------------------------------------------------------

class TestCloneDocs:
    def test_env_example_documents_mcp_alias_and_yaml_flag(self):
        text = (ROOT / ".env.example").read_text()
        assert "MCP_GATEWAY_URL" in text
        assert "AGENTCORE_GATEWAY_URL" in text
        assert "YAML_PLAYBOOKS_ENABLED=false" in text
        assert "AGENTCORE_TARGET_SPLUNK" in text
        assert "WORKER_ALIASES_PATH" in text

    def test_connect_doc_exists(self):
        doc = ROOT / "docs" / "clone" / "CONNECT_YOUR_ENVIRONMENT.md"
        text = doc.read_text()
        assert "MCP_GATEWAY_URL" in text
        assert "AGENTCORE_TARGET_" in text
        assert "YAML_PLAYBOOKS_ENABLED" in text
        assert "worker_aliases" in text
