"""Plain-MCP mode, connection diagnostics, timeouts, and the model guard.

Existing mcp_client assertions are not modified. Flag off keeps AgentCore
names and unconfigured stub bytes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from supervisor.agent import SentinalAISupervisor
from workers import mcp_client as mc
from workers.mcp_client import McpGateway, _stub_response, outbound_tool_name
from workers.mcp_diagnostics import (
    HASH_EXCLUSIONS,
    diagnose,
    format_json,
    format_text,
    replay_canonical,
    replay_hash,
    report_exit_code,
)


def _live_gateway() -> McpGateway:
    gateway = McpGateway.__new__(McpGateway)
    gateway._mcp_client = None
    gateway._boto3_client = None
    gateway._tools_cache = None
    gateway._oauth2_provider = None
    gateway._call_signatures = set()
    gateway._rate_limiter = MagicMock()
    gateway._rate_limiter.acquire.return_value = True
    gateway._current_user_identity = None
    return gateway


@pytest.fixture
def plain_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAIN_MCP", raising=False)
    monkeypatch.setattr(mc, "GATEWAY_MODE", "")
    monkeypatch.setattr(mc, "AGENTCORE_GATEWAY_URL", "")


class TestPlainToolNames:
    def test_flag_off_keeps_target_rewrite(self, plain_off: None) -> None:
        assert outbound_tool_name("splunk.search_oneshot") == "SplunkTarget___search_oneshot"
        assert outbound_tool_name("moogsoft.get_incident_by_id") == "MoogsoftTarget___get_incident_by_id"

    def test_flag_on_sends_plain_name(self, monkeypatch: pytest.MonkeyPatch, plain_off: None) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        assert outbound_tool_name("splunk.search_oneshot") == "splunk.search_oneshot"
        assert outbound_tool_name("github.get_pr_details") == "github.get_pr_details"

    def test_invoke_uses_rewritten_name_when_flag_off(self, plain_off: None) -> None:
        gateway = _live_gateway()
        client = MagicMock()
        client.call_tool_sync.return_value = {"logs": {"results": [], "count": 0}}
        gateway._mcp_client = client
        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            gateway.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        assert client.call_tool_sync.call_args.kwargs["name"] == "SplunkTarget___search_oneshot"

    def test_invoke_uses_plain_name_when_flag_on(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        gateway = _live_gateway()
        client = MagicMock()
        client.call_tool_sync.return_value = {"logs": {"results": [], "count": 0}}
        gateway._mcp_client = client
        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            gateway.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        assert client.call_tool_sync.call_args.kwargs["name"] == "splunk.search_oneshot"


class TestFailureDoesNotStub:
    def test_endpoint_down_is_failed_without_stub_data(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        gateway = _live_gateway()
        client = MagicMock()
        client.call_tool_sync.side_effect = ConnectionError("connection refused")
        gateway._mcp_client = client
        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            result = gateway.invoke("splunk.search_oneshot", "search_logs", {})
        assert result["connection_state"] == "failed"
        assert result["error_class"] == "ConnectionError"
        assert "logs" not in result
        assert "changes" not in result

    def test_client_cannot_be_built_is_failed_in_plain_mode(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        gateway = _live_gateway()
        with patch.object(gateway, "_get_mcp_client", return_value=None), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            result = gateway.invoke("moogsoft.get_incident_by_id", "get_incident_by_id", {})
        assert result["connection_state"] == "failed"
        assert "incident" not in result

    def test_flag_off_client_failure_still_stubs_and_labels(self, plain_off: None) -> None:
        gateway = _live_gateway()
        with patch.object(gateway, "_get_mcp_client", return_value=None), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            result = gateway.invoke(
                "splunk.search_oneshot", "search_logs", {"query": "x"},
            )
        assert result["connection_state"] == "stubbed"
        assert "logs" in result

    def test_unconfigured_stub_bytes_unchanged(self, plain_off: None) -> None:
        gateway = McpGateway()
        result = gateway.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})
        assert result == _stub_response("splunk.search_oneshot", "search_logs", {"query": "x"})
        assert "connection_state" not in result

    def test_call_past_timeout_fails_within_bound(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0.3")
        gateway = _live_gateway()
        client = MagicMock()

        def _hang(*_args: object, **_kwargs: object) -> dict:
            time.sleep(5)
            return {"logs": {"results": [1]}}

        client.call_tool_sync.side_effect = _hang
        gateway._mcp_client = client
        started = time.monotonic()
        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            result = gateway.invoke("splunk.search_oneshot", "search_logs", {})
        elapsed = time.monotonic() - started
        assert elapsed < 0.3 + 2
        assert result["connection_state"] == "failed"
        assert result["error_class"] == "TimeoutError"
        assert "logs" not in result

    def test_two_401s_retry_once_then_fail(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        provider = MagicMock()
        provider.get_auth_headers.return_value = {"Authorization": "Bearer super-secret-token"}
        gateway = _live_gateway()
        gateway._oauth2_provider = provider
        client = MagicMock()
        client.call_tool_sync.side_effect = ConnectionError("HTTP 401 Unauthorized Bearer super-secret-token")
        with patch.object(gateway, "_get_mcp_client", return_value=client), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True):
            result = gateway.invoke("github.get_recent_deployments", "get_recent_deployments", {})
        assert client.call_tool_sync.call_count == 2
        provider.invalidate.assert_called_once()
        assert result["connection_state"] == "failed"
        assert "deployments" not in result
        assert "super-secret-token" not in json.dumps(result)


class TestModelGuard:
    def test_plain_mode_refuses_non_null_model(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        gateway = _live_gateway()
        client = MagicMock()
        gateway._mcp_client = client

        class LivePort:
            pass

        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True), \
             patch("supervisor.llm.get_inference_port", return_value=LivePort()):
            result = gateway.invoke("splunk.search_oneshot", "search_logs", {})
        client.call_tool_sync.assert_not_called()
        assert result["error_class"] == "PlainMcpModelGuard"
        assert result["connection_state"] == "failed"
        assert "identifier masking" in result["error"]

    def test_null_model_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, plain_off: None,
    ) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        monkeypatch.setattr(mc, "LLM_ENABLED", False, raising=False)
        gateway = _live_gateway()
        client = MagicMock()
        client.call_tool_sync.return_value = {"logs": {"results": [], "count": 0}}
        gateway._mcp_client = client

        class NullInference:
            pass

        with patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True), \
             patch("supervisor.llm.get_inference_port", return_value=NullInference()):
            result = gateway.invoke("splunk.search_oneshot", "search_logs", {})
        assert result == {"logs": {"results": [], "count": 0}}
        client.call_tool_sync.assert_called_once()


_OPS = {
    "ops_worker": frozenset({"moogsoft"}),
    "log_worker": frozenset({"splunk"}),
    "itsm_worker": frozenset({"servicenow"}),
    "knowledge_worker": frozenset(),
}


class TestDiagnosticsStates:
    def test_stubbed_when_nothing_configured(self, plain_off: None) -> None:
        report = diagnose(workers=_OPS, configured=False)
        assert [row["worker"] for row in report["workers"]] == sorted(_OPS)
        assert {row["state"] for row in report["workers"]} == {"stubbed"}
        assert report_exit_code(report) == 0
        text = format_text(report)
        assert "stubbed" in text
        blob = format_json(report)
        assert json.loads(blob) == json.loads(format_json(report))
        assert "timestamp" not in blob

    def test_unlisted_tool_is_missing(self) -> None:
        report = diagnose(
            workers={"ops_worker": frozenset({"moogsoft"}), "itsm_worker": frozenset({"servicenow"})},
            tools_for={
                "ops_worker": ("moogsoft.get_incident_by_id",),
                "itsm_worker": ("servicenow.get_ci_details",),
            },
            list_tools=lambda: ["MoogsoftTarget___get_incident_by_id"],
            call_tool=lambda _name: {"incident": {"incident_id": "INC-OSS-001"}},
            configured=True,
            assume_endpoint=True,
        )
        by_name = {row["worker"]: row for row in report["workers"]}
        assert by_name["ops_worker"]["state"] == "reachable"
        assert by_name["itsm_worker"]["state"] == "missing"
        assert "unlisted" in by_name["itsm_worker"]["reason"]
        assert report_exit_code(report) == 2

    def test_skipped_tool_is_missing_not_stubbed(self) -> None:
        report = diagnose(
            workers={"confluence_worker": frozenset({"confluence"})},
            tools_for={"confluence_worker": ("confluence.search_runbooks",)},
            list_tools=lambda: ["ConfluenceTarget___search_runbooks"],
            call_tool=lambda _name: {"skipped": True, "runbooks": []},
            configured=True,
            assume_endpoint=True,
        )
        row = report["workers"][0]
        assert row["state"] == "missing"
        assert row["state"] != "stubbed"
        assert "not_provided" in row["reason"]

    def test_endpoint_down_fails_every_required_worker(self) -> None:
        def _down() -> list[str]:
            raise ConnectionError("connection refused")

        report = diagnose(
            workers=_OPS,
            list_tools=_down,
            call_tool=lambda _name: {},
            configured=True,
            assume_endpoint=True,
        )
        required = [row for row in report["workers"] if row["required"]]
        assert required
        assert {row["state"] for row in required} == {"failed"}
        assert {row["error_class"] for row in required} == {"ConnectionError"}
        assert report["workers"][[r["worker"] for r in report["workers"]].index("knowledge_worker")]["state"] == "reachable"
        assert report_exit_code(report) == 2

    def test_list_timeout_is_failed_within_bound(self) -> None:
        def _slow() -> list[str]:
            time.sleep(5)
            return []

        started = time.monotonic()
        report = diagnose(
            workers={"ops_worker": frozenset({"moogsoft"})},
            tools_for={"ops_worker": ("moogsoft.get_incident_by_id",)},
            list_tools=_slow,
            call_tool=lambda _name: {},
            timeout_s=0.3,
            configured=True,
            assume_endpoint=True,
        )
        assert time.monotonic() - started < 0.3 + 2
        assert report["workers"][0]["state"] == "failed"
        assert report["workers"][0]["error_class"] == "TimeoutError"

    def test_no_endpoint_is_missing(self, plain_off: None) -> None:
        report = diagnose(
            workers={"itsm_worker": frozenset({"servicenow"})},
            configured=True,
            assume_endpoint=False,
        )
        assert report["workers"][0]["state"] == "missing"
        assert report["workers"][0]["reason"] == "no endpoint configured"

    def test_secrets_are_scrubbed(self) -> None:
        def _down() -> list[str]:
            raise ConnectionError("failed Authorization: Bearer super-secret-token")

        report = diagnose(
            workers={"ops_worker": frozenset({"moogsoft"})},
            list_tools=_down,
            configured=True,
            assume_endpoint=True,
        )
        blob = format_json(report) + format_text(report)
        assert "super-secret-token" not in blob
        assert "[redacted]" in blob


# Investigation-only hash of INC12345 at e364e32 with the committed frozen
# corpus (pattern registry, evolved strategy, experience store, knowledge
# graph) and LLM/calibration off, GATEWAY_MODE=stub. Learning writes after a
# run are visible to the next capture, so the three runs pin that corpus.
_INC12345_INVESTIGATION_HASH = "e528c42a50b4aaf30280c2418e9a8924178c7697bd76de4da928604f109d5f72"


# Committed paths. tests/conftest.py redirects the import-time env copies to an
# empty temp dir and deletes them before each test, so run 2 would otherwise
# observe run 1's writes. These names are the four Frozen Corpus stores.
_COMMITTED_CORPUS = {
    "pattern_registry": "eval/pattern_registry.json",
    "evolved_strategy": "eval/evolved_strategy.json",
    "experience": "eval/experience_store.json",
    "knowledge_graph": "eval/knowledge_graph.json",
}


# Read during an investigation, outside the Frozen Corpus. conftest points the
# import-time copies at an empty temp dir, so a second run would see the first
# run's writes. Each investigate() restores these bytes first.
_LIVE_READS = (
    # Worker threads do not see the thread-local Frozen Corpus, so they read
    # these live paths. Restore them before every investigate().
    ("supervisor.experience_store", "EXPERIENCE_STORE_PATH", "eval/experience_store.json"),
    ("supervisor.knowledge_graph", "KG_PATH", "eval/knowledge_graph.json"),
    ("supervisor.strategy_evolver", "EVOLVED_STRATEGY_PATH", "eval/evolved_strategy.json"),
    ("supervisor.gap_aggregator", "GAP_AGGREGATOR_PATH", "eval/gap_patterns.json"),
    ("supervisor.adaptive_thresholds", "ADAPTIVE_THRESHOLDS_PATH", "eval/adaptive_thresholds.json"),
    ("supervisor.recurrence_tracker", "RECURRENCE_INDEX_PATH", "eval/recurrence_index.json"),
    ("supervisor.blast_radius_history", "_DEFAULT_STORE", "eval/blast_radius_history.json"),
    ("intelligence.episodic_memory", "_DEFAULT_STORAGE_PATH", "eval/episodic_memory.jsonl"),
)


def _git_bytes(rel: str) -> bytes | None:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "show", f"HEAD:{rel}"], stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None


def _pin_committed_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes | None]:
    """Point store reads at committed bytes. Missing files stay missing.

    ``eval/adaptive_thresholds.json`` is gitignored, so CI has no copy. A
    missing file uses the module defaults. Snapshots are restored before
    every investigate() so run 2 does not observe run 1's writes.
    """
    import importlib

    import supervisor.frozen_corpus as fc

    pinned: dict[str, str] = {}
    snapshots: dict[str, bytes | None] = {}
    for key, rel in _COMMITTED_CORPUS.items():
        dst = tmp_path / f"corpus-{key}.json"
        data = _git_bytes(rel)
        if data is None:
            dst.unlink(missing_ok=True)
        else:
            dst.write_bytes(data)
        pinned[key] = str(dst)
        snapshots[str(dst)] = data
    monkeypatch.setattr(fc, "_STORE_PATHS", pinned)

    for module_name, attr, rel in _LIVE_READS:
        dst = tmp_path / Path(rel).name
        data = _git_bytes(rel)
        if data is None:
            dst.unlink(missing_ok=True)
        else:
            dst.write_bytes(data)
        snapshots[str(dst)] = data
        monkeypatch.setattr(importlib.import_module(module_name), attr, str(dst))
    return snapshots


def _reset_learning_snapshots(snapshots: dict[str, bytes | None]) -> None:
    import subprocess

    import supervisor.blast_radius_history as blast
    import supervisor.knowledge_graph as kg

    subprocess.check_call(
        ["git", "checkout", "--", "sentinel_wiki/patterns"],
        stdout=subprocess.DEVNULL,
    )
    for path, data in snapshots.items():
        target = Path(path)
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(data)
    blast._history = None
    kg._graph = None


class TestInc12345Unchanged:
    def test_flag_off_replay_hash_matches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.delenv("PLAIN_MCP", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "false")
        monkeypatch.setenv("CALIBRATION_ENABLED", "false")
        monkeypatch.setenv("GATEWAY_MODE", "stub")
        monkeypatch.setattr(mc, "GATEWAY_MODE", "stub")
        monkeypatch.setattr(mc, "AGENTCORE_GATEWAY_URL", "")
        monkeypatch.setattr("supervisor.llm.LLM_ENABLED", False)
        monkeypatch.setattr("supervisor.confidence_calibrator.CALIBRATION_ENABLED", False)
        snapshots = _pin_committed_corpus(tmp_path, monkeypatch)

        _reset_learning_snapshots(snapshots)
        sup1 = SentinalAISupervisor(replay_dir=str(tmp_path / "a"))
        first = sup1.investigate("INC12345")
        _reset_learning_snapshots(snapshots)
        second = SentinalAISupervisor(replay_dir=str(tmp_path / "b")).investigate("INC12345")
        _reset_learning_snapshots(snapshots)
        replayed = sup1.investigate("INC12345", replay=True)
        report = diagnose()

        _doc, missing = replay_canonical(first, report)
        assert "incident_type" in missing
        assert "hypothesis_ranking" in missing
        assert "tool_call_sequence" in missing
        assert HASH_EXCLUSIONS

        hash_first = replay_hash(first, report)
        hash_second = replay_hash(second, report)
        hash_replay = replay_hash(replayed, report)
        assert hash_first == hash_second == hash_replay

        # Investigation fields only — comparable to e364e32, before this report existed.
        investigation_only = replay_hash(first, None)
        assert investigation_only == replay_hash(second, None) == replay_hash(replayed, None)
        assert investigation_only == _INC12345_INVESTIGATION_HASH
