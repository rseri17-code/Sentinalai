"""Tests for supervisor/policy_gate.py — pre-dispatch policy gate."""
from __future__ import annotations

import importlib
import os
from unittest.mock import MagicMock, patch

import pytest

import supervisor.policy_gate as pg
from supervisor.policy_gate import PolicyDecision, PolicyGate, PolicyResult, evaluate


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _fresh_gate() -> PolicyGate:
    return PolicyGate()


# ---------------------------------------------------------------------------
# 1. Gate disabled (default) — always ALLOW
# ---------------------------------------------------------------------------

def test_allow_when_flag_off(monkeypatch):
    monkeypatch.delenv("POLICY_GATE_ENABLED", raising=False)
    gate = _fresh_gate()
    result = gate.evaluate("github.create_pr", params=None, budget_remaining=0)
    assert result.allowed
    assert result.decision == PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# 2. Gate enabled, valid dict params → ALLOW
# ---------------------------------------------------------------------------

def test_allow_valid_params_flag_on(monkeypatch):
    monkeypatch.setenv("POLICY_GATE_ENABLED", "true")
    gate = _fresh_gate()
    result = gate.evaluate("splunk.search_oneshot", params={"query": "error"}, budget_remaining=10)
    assert result.allowed


# ---------------------------------------------------------------------------
# 3. Reject non-dict params
# ---------------------------------------------------------------------------

def test_reject_non_dict_params(monkeypatch):
    monkeypatch.setenv("POLICY_GATE_ENABLED", "true")
    gate = _fresh_gate()
    result = gate.evaluate("splunk.search_oneshot", params=None)
    assert not result.allowed
    assert result.decision == PolicyDecision.REJECT
    assert "NoneType" in result.reason


# ---------------------------------------------------------------------------
# 4. Reject when budget exhausted
# ---------------------------------------------------------------------------

def test_reject_budget_exhausted(monkeypatch):
    monkeypatch.setenv("POLICY_GATE_ENABLED", "true")
    gate = _fresh_gate()
    result = gate.evaluate("splunk.search_oneshot", params={}, budget_remaining=0)
    assert not result.allowed
    assert result.decision == PolicyDecision.REJECT
    assert "Budget exhausted" in result.reason


# ---------------------------------------------------------------------------
# 5. Allow when budget is positive
# ---------------------------------------------------------------------------

def test_allow_budget_remaining(monkeypatch):
    monkeypatch.setenv("POLICY_GATE_ENABLED", "true")
    gate = _fresh_gate()
    result = gate.evaluate("splunk.search_oneshot", params={}, budget_remaining=5)
    assert result.allowed


# ---------------------------------------------------------------------------
# 6. Module-level evaluate() delegates to the singleton PolicyGate
# ---------------------------------------------------------------------------

def test_evaluate_module_function(monkeypatch):
    monkeypatch.setenv("POLICY_GATE_ENABLED", "true")
    result = evaluate("splunk.search_oneshot", params={"q": "test"}, budget_remaining=10)
    assert result.allowed


# ---------------------------------------------------------------------------
# 7. PolicyResult.allowed property
# ---------------------------------------------------------------------------

def test_policy_result_allowed_property():
    allow_result = PolicyResult(decision=PolicyDecision.ALLOW)
    reject_result = PolicyResult(decision=PolicyDecision.REJECT, reason="test")
    replan_result = PolicyResult(decision=PolicyDecision.REPLAN)
    assert allow_result.allowed is True
    assert reject_result.allowed is False
    assert replan_result.allowed is False


# ---------------------------------------------------------------------------
# 8. Integration: McpGateway.invoke passes through normally when gate is off
# ---------------------------------------------------------------------------

def test_mcp_gateway_passes_through_when_gate_off(monkeypatch):
    """When POLICY_GATE_ENABLED=false, invoke() should reach the stub/gateway path."""
    monkeypatch.delenv("POLICY_GATE_ENABLED", raising=False)

    from workers.mcp_client import McpGateway

    gateway = McpGateway.__new__(McpGateway)

    # Minimal attribute setup to avoid __init__ side effects
    gateway._current_user_identity = None

    # Patch _rate_limiter to always pass
    mock_limiter = MagicMock()
    mock_limiter.acquire.return_value = True
    gateway._rate_limiter = mock_limiter

    # Patch _stub_response to return a known sentinel
    sentinel = {"status": "stub_ok"}
    with patch("workers.mcp_client._stub_response", return_value=sentinel) as mock_stub, \
         patch("workers.mcp_client.AGENTCORE_GATEWAY_URL", ""), \
         patch.object(gateway, "get_arn_for_tool", return_value=None):

        result = gateway.invoke("splunk.search_oneshot", "search_oneshot", {"query": "test"})

    assert result == sentinel
    mock_stub.assert_called_once()
