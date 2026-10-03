"""Strands-shaped MCP client contract for GitHub #89.

The fake client is not a MagicMock: ``call_tool_sync`` raises unless
``start()`` has run, and every tool result is a real ToolResult dict
(``toolUseId``, ``status``, ``content``). These cases are the ones that
still fail on main ``ff658c7`` (PR #85). Single-call session start and
JSON-object unwrap already succeed there and are covered separately.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from workers import mcp_client as mc
from workers.mcp_client import McpGateway


class NullInference:
    """Name must be NullInference so the plain-MCP model guard allows the call."""


class FakeStrandsClient:
    """Minimal strands ``MCPClient`` semantics used by live invoke."""

    def __init__(self, transport: Any, result: Any, *, stall_s: float = 0.0) -> None:
        self.transport = transport
        self._result = result
        self._stall_s = stall_s
        self.started = False
        self.start_calls = 0
        if stall_s:
            time.sleep(stall_s)

    def start(self) -> FakeStrandsClient:
        if self.started:
            raise RuntimeError("the client session is currently running")
        self.start_calls += 1
        self.started = True
        return self

    def call_tool_sync(self, **_kwargs: Any) -> dict[str, Any]:
        if not self.started:
            raise RuntimeError("the client session is not running")
        return self._result


def _tool_result(text: str, status: str = "success") -> dict[str, Any]:
    return {
        "status": status,
        "toolUseId": "tooluse-strands-1",
        "content": [{"text": text}],
    }


def _gateway() -> McpGateway:
    gateway = McpGateway.__new__(McpGateway)
    gateway._mcp_client = None
    gateway._boto3_client = None
    gateway._tools_cache = None
    gateway._oauth2_provider = None
    gateway._call_signatures = set()
    gateway._rate_limiter = type("R", (), {"acquire": lambda self, *_a, **_k: True})()
    gateway._current_user_identity = None
    return gateway


def _invoke(gateway: McpGateway, factory: Any) -> dict[str, Any]:
    with patch.object(mc, "MCPClient", side_effect=factory), \
         patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
         patch.object(mc, "_MCP_SDK_AVAILABLE", True), \
         patch.object(mc, "GATEWAY_MODE", ""), \
         patch("supervisor.llm.get_inference_port", return_value=NullInference()):
        return gateway.invoke("splunk.search_oneshot", "search_logs", {"query": "x"})


@pytest.fixture(params=[False, True], ids=["flag-off", "plain-mcp"])
def plain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    if request.param:
        monkeypatch.setenv("PLAIN_MCP", "true")
    else:
        monkeypatch.delenv("PLAIN_MCP", raising=False)
    return bool(request.param)


class TestStatusErrorMapping:
    """A strands status ``error`` ToolResult must not be returned as evidence."""

    def test_text_error_maps_to_tool_status_error(self, plain: bool) -> None:
        detail = "Tool execution failed: boom"
        gateway = _gateway()
        result = _invoke(
            gateway,
            lambda transport: FakeStrandsClient(transport, _tool_result(detail, status="error")),
        )
        assert result == {
            "error": f"tool_status_error: {detail}",
            "tool_status": "error",
        }

    def test_json_object_error_is_not_evidence(self, plain: bool) -> None:
        text = '{"logs": {"results": [{"message": "boom"}]}, "count": 1}'
        gateway = _gateway()
        result = _invoke(
            gateway,
            lambda transport: FakeStrandsClient(transport, _tool_result(text, status="error")),
        )
        assert result == {
            "error": f"tool_status_error: {text}",
            "tool_status": "error",
        }
        assert "logs" not in result
        assert "toolUseId" not in result
        assert "content" not in result


class TestConcurrentSessionStart:
    """Parallel first invokes must open one session, not one per worker."""

    def test_start_called_once(self, plain: bool) -> None:
        built: list[FakeStrandsClient] = []
        payload = {"logs": {"results": [{"message": "ok"}], "count": 1}}
        envelope = _tool_result(json.dumps(payload))

        def factory(transport: Any) -> FakeStrandsClient:
            client = FakeStrandsClient(transport, envelope, stall_s=0.2)
            built.append(client)
            return client

        gateway = _gateway()
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def one() -> None:
            try:
                results.append(gateway.invoke("splunk.search_oneshot", "search_logs", {"query": "x"}))
            except BaseException as exc:  # surface races instead of hiding them
                errors.append(exc)

        with patch.object(mc, "MCPClient", side_effect=factory), \
             patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
             patch.object(mc, "_MCP_SDK_AVAILABLE", True), \
             patch.object(mc, "GATEWAY_MODE", ""), \
             patch("supervisor.llm.get_inference_port", return_value=NullInference()):
            threads = [threading.Thread(target=one) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert errors == []
        assert len(built) == 1
        assert built[0].start_calls == 1
        assert results == [payload] * 8
