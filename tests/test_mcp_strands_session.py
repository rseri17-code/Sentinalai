"""v1.9 strands session contract for GitHub #89.

The fake client is not a MagicMock. ``call_tool_sync`` raises unless
``start()`` has finished, ``start()`` takes long enough for concurrent
first calls to overlap, and tool results are ToolResult dicts
(``toolUseId`` / ``status`` / ``content``).

These tests are the pass bar against main ``ff658c7`` before the fix.
They do not require the retired out-of-tree adapter's error shape.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from workers import mcp_client as mc
from workers.mcp_client import McpGateway


class NullInference:
    """Name must be NullInference so the plain-MCP model guard allows the call."""


class _ThreadFatal(BaseException):
    """Raised on the timeout worker. Not an Exception, so ``except Exception`` misses it."""


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


@contextmanager
def _live(factory: Any) -> Iterator[None]:
    with patch.object(mc, "MCPClient", side_effect=factory), \
         patch.object(mc, "AGENTCORE_GATEWAY_URL", "https://gw.example/mcp"), \
         patch.object(mc, "_MCP_SDK_AVAILABLE", True), \
         patch.object(mc, "GATEWAY_MODE", ""), \
         patch("supervisor.llm.get_inference_port", return_value=NullInference()):
        yield


def _invoke(gateway: McpGateway, factory: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _live(factory):
        return gateway.invoke(
            "splunk.search_oneshot", "search_logs", {"query": "x"} if params is None else params,
        )


@pytest.fixture(params=[False, True], ids=["flag-off", "plain-mcp"])
def plain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    if request.param:
        monkeypatch.setenv("PLAIN_MCP", "true")
    else:
        monkeypatch.delenv("PLAIN_MCP", raising=False)
    return bool(request.param)


class _SlowStartClient:
    """start() sleeps so other first calls observe the client mid-start."""

    def __init__(self, transport: Any, result: dict[str, Any], started_box: list[_SlowStartClient]) -> None:
        self.transport = transport
        self._result = result
        self.started = False
        self.start_calls = 0
        started_box.append(self)

    def start(self) -> _SlowStartClient:
        self.start_calls += 1
        time.sleep(0.3)
        self.started = True
        return self

    def call_tool_sync(self, **_kwargs: Any) -> dict[str, Any]:
        if not self.started:
            raise RuntimeError("the client session is not running")
        return self._result


class TestConcurrentFirstCalls:
    """v1.9 item 1."""

    def test_eight_concurrent_first_calls_one_session(self, plain: bool) -> None:
        payload = {"logs": {"results": [{"message": "ok"}], "count": 1}}
        envelope = _tool_result(json.dumps(payload))
        built: list[_SlowStartClient] = []
        gateway = _gateway()

        def factory(transport: Any) -> _SlowStartClient:
            return _SlowStartClient(transport, envelope, built)

        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def one(i: int) -> None:
            try:
                results.append(
                    gateway.invoke("splunk.search_oneshot", "search_logs", {"query": f"q{i}"}),
                )
            except BaseException as exc:
                errors.append(exc)

        with _live(factory):
            threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        blob = json.dumps(results, default=str)
        assert errors == []
        assert "not running" not in blob
        assert "could not be built" not in blob
        assert "stubbed" not in blob
        assert sum(client.start_calls for client in built) == 1
        assert len(built) == 1
        assert results == [payload] * 8


class _StartFails:
    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.calls = 0
        self.start_thread: int | None = None

    def start(self) -> None:
        self.start_thread = threading.get_ident()
        raise ConnectionError("dial tcp refused")

    def call_tool_sync(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("call_tool_sync must not run after a failed start")


class TestFailedStart:
    """v1.9 items 3 and 5."""

    def test_plain_failed_start_is_loud_on_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLAIN_MCP", "true")
        client = _StartFails(None)
        gateway = _gateway()
        caller = threading.get_ident()
        result = _invoke(gateway, lambda _transport: client)
        assert client.start_thread is not None
        assert client.start_thread != caller
        assert client.calls == 0
        assert result.get("connection_state") == "failed"
        assert result.get("error_class") == "ConnectionError"
        assert "dial tcp refused" in str(result.get("error"))
        assert "logs" not in result
        assert "stubbed" not in json.dumps(result)
        assert result.get("raw_response") != "None"

    def test_flag_off_failed_start_still_stubs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLAIN_MCP", raising=False)
        client = _StartFails(None)
        gateway = _gateway()
        result = _invoke(gateway, lambda _transport: client)
        assert client.calls == 0
        assert result.get("connection_state") == "stubbed"
        assert "logs" in result


class _FatalCall:
    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.started = False

    def start(self) -> _FatalCall:
        self.started = True
        return self

    def call_tool_sync(self, **_kwargs: Any) -> dict[str, Any]:
        if not self.started:
            raise RuntimeError("the client session is not running")
        raise _ThreadFatal("background thread died")


class TestTimeoutThreadBaseException:
    """v1.9 items 2 and 4 (main error fields stay visible)."""

    def test_base_exception_is_not_raw_none(self, plain: bool) -> None:
        gateway = _gateway()
        result = _invoke(gateway, lambda transport: _FatalCall(transport))
        assert result.get("raw_response") != "None"
        assert "raw_response" not in result
        assert "background thread died" in str(result.get("error"))
        if plain:
            assert result.get("connection_state") == "failed"
            assert str(result.get("error", "")).startswith("failed:")
        else:
            assert str(result.get("error", "")).startswith("gateway_exception:")
            assert "connection_state" not in result


class TestUnwrapOnce:
    """v1.9 item 4. A tool payload that is itself an envelope is not opened again."""

    def test_envelope_shaped_payload_unwrapped_once(self, plain: bool) -> None:
        inner = {
            "toolUseId": "inner-tool",
            "status": "success",
            "content": [{"text": json.dumps({"logs": {"results": [{"message": "deep"}]}})}],
            "error": "gateway_exception: kept",
            "connection_state": "failed",
        }
        outer = _tool_result(json.dumps(inner))

        class _Client:
            def __init__(self, transport: Any) -> None:
                self.transport = transport
                self.started = False

            def start(self) -> _Client:
                self.started = True
                return self

            def call_tool_sync(self, **_kwargs: Any) -> dict[str, Any]:
                if not self.started:
                    raise RuntimeError("the client session is not running")
                return outer

        result = _invoke(_gateway(), lambda transport: _Client(transport))
        assert result == inner
        assert result["error"] == "gateway_exception: kept"
        assert result["connection_state"] == "failed"
        assert "logs" not in result
