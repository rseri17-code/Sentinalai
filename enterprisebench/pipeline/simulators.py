"""EB-3 — dedicated MCP simulators for the Enterprise Digital Twin.

REACHABILITY IS THE LOAD-BEARING CONSTRAINT. Under the mission's hard rule "do not
modify the investigation engine", a simulator is only useful if the UNMODIFIED
engine has a query path to the MCP. The EB-3 reachability analysis (see
`docs/enterprisebench/ARCHITECTURE.md` §9c) established, with cites:

  * ThousandEyes  — REACHABLE via `ENABLE_THOUSANDEYES_RCA=true`. The engine's
    `network_worker` + the timeout/latency/network playbooks already query it
    (through the `integrations.thousandeyes.adapter`, not the MCP gateway).
  * certificates, route53/DNS, identity/IAM, aws_cloudwatch, autosys — NOT
    reachable: no worker, no playbook step, no flag. The engine cannot query them
    without adding a worker + playbook step (an engine code change the mission
    forbids). A dedicated simulator for them would be UNCONSUMED — building it
    would be fidelity theater, so we do not.
  * cmdb — not a distinct MCP: served through `servicenow.get_ci_details` (already
    supported). EB-3 routes cmdb evidence there (its production equivalent) rather
    than folding it into Splunk logs.

This module therefore implements the ONE reachable dedicated simulator
(ThousandEyes) as a proper, deterministic, production-shaped component.
"""
from __future__ import annotations

from typing import Any, Mapping

# Deterministic identities (no clock, no randomness).
_TS_START = "2024-01-01T00:00:00Z"
_TS_END = "2024-01-01T00:30:00Z"
_TEST_ID = 900001


def _alert(test_name: str, test_type: str, rule: str, severity: str,
           agents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "alertId": 990001, "testId": _TEST_ID, "testName": test_name,
        "type": test_type, "active": 1, "severity": severity,
        "dateStart": _TS_START, "dateEnd": _TS_END,
        "alertRule": {"alertRuleId": 11001, "alertRuleName": rule},
        "agents": agents,
    }


def _agent(name: str, availability: float, loss: float = 0.0) -> dict[str, Any]:
    return {"agentId": 10001, "agentName": name, "availability": availability,
            "loss": loss, "roundId": 1}


def thousandeyes_responses(te: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deterministic ThousandEyes responses for one scenario's TE telemetry.

    Returns the three adapter responses the engine's network_worker consumes:
    ``list_alerts`` → ``{"alerts":[…]}``, ``get_test_results`` →
    ``{"type","results":[…]}``, ``list_tests`` → ``{"tests":[…]}``. Positive
    signal (packet loss / DNS failure / TLS error) yields an active alert with
    unhealthy agents; a healthy probe (``{"ok":true}``) yields NO active alerts —
    the genuine negative evidence that lets the engine RULE OUT the network.
    """
    te = te or {}
    tests = {"tests": [{"testId": _TEST_ID, "testName": "twin-probe",
                        "type": "agent-to-server"}]}

    if te.get("packet_loss") is not None:
        loss = float(te.get("packet_loss", 0) or 0)
        agents = [_agent("New York", 100 * (1 - loss), loss * 100),
                  _agent("Chicago", 100 * (1 - loss), loss * 100),
                  _agent("Frankfurt", 100.0, 0.0)]
        alert = _alert(f"Network Path — {te.get('path','edge->core')}",
                       "agent-to-server", "Packet loss > 10%", "major", agents)
        results = {"type": "agent-to-server", "testId": _TEST_ID,
                   "results": [{"testId": _TEST_ID, "agentId": a["agentId"],
                                "agentName": a["agentName"], "loss": a["loss"],
                                "availability": a["availability"]} for a in agents]}
        return {"list_alerts": {"alerts": [alert]}, "get_test_results": results,
                "list_tests": tests}

    if str(te.get("dns_test", "")).lower() == "failing" or te.get("resolver"):
        agents = [_agent("New York", 0.0), _agent("Chicago", 0.0)]
        alert = _alert("DNS Resolution", "dns-server",
                       "DNS resolution failing", "critical", agents)
        results = {"type": "dns-server", "testId": _TEST_ID,
                   "results": [{"testId": _TEST_ID, "agentId": a["agentId"],
                                "agentName": a["agentName"], "availability": 0.0,
                                "errorType": "DNS"} for a in agents]}
        return {"list_alerts": {"alerts": [alert]}, "get_test_results": results,
                "list_tests": tests}

    if te.get("tls_error"):
        agents = [_agent("New York", 0.0)]
        alert = _alert("HTTP/TLS", "http-server", "TLS handshake failure",
                       "critical", agents)
        results = {"type": "http-server", "testId": _TEST_ID,
                   "results": [{"testId": _TEST_ID, "agentId": 10001,
                                "agentName": "New York", "availability": 0.0,
                                "errorType": "TLS"}]}
        return {"list_alerts": {"alerts": [alert]}, "get_test_results": results,
                "list_tests": tests}

    # Healthy probe (or no TE telemetry): no active alerts → network is ruled out.
    return {"list_alerts": {"alerts": []},
            "get_test_results": {"type": "agent-to-server", "results": []},
            "list_tests": tests}


# MCPs that are part of the EFIC corpus but that the UNMODIFIED engine cannot
# query (no worker/playbook/flag). Documented, not simulated (see module docstring).
ENGINE_UNREACHABLE_MCPS = (
    "certificates", "route53_dns", "identity", "aws_cloudwatch", "autosys",
)

__all__ = ["thousandeyes_responses", "ENGINE_UNREACHABLE_MCPS"]
