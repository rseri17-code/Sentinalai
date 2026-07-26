"""EB-3 — Enterprise Digital Twin: dedicated MCP simulators + folding removal.

Reachability is the load-bearing constraint (see docs/enterprisebench §9c): the
unmodified engine can query only ThousandEyes (via a flag); the other EFIC MCPs
have no query path. These tests pin the honest behaviour: the ThousandEyes
simulator is deterministic and production-shaped; folding is removed; CMDB is
routed to its ServiceNow production equivalent; and the engine-unreachable MCPs
are documented, not folded.
"""
from __future__ import annotations

import json
import os

from enterprisebench.pipeline.render import ENGINE_UNREACHABLE, render
from enterprisebench.pipeline.simulators import (ENGINE_UNREACHABLE_MCPS,
                                                 thousandeyes_responses)

_CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "eval", "efic", "corpus.json")


def _corpus():
    with open(_CORPUS) as f:
        return json.load(f)


def _entry(tid):
    return next(e for e in _corpus()["corpus"] if e["task"]["task_id"] == tid)


# --- ThousandEyes simulator ------------------------------------------------

def test_thousandeyes_deterministic():
    a = thousandeyes_responses({"packet_loss": 0.18, "path": "edge->core"})
    b = thousandeyes_responses({"packet_loss": 0.18, "path": "edge->core"})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_thousandeyes_healthy_has_no_active_alerts():
    """A healthy probe ({"ok":true}) yields NO alerts — genuine negative evidence
    that lets the engine rule out the network."""
    r = thousandeyes_responses({"ok": True})
    assert r["list_alerts"]["alerts"] == []


def test_thousandeyes_positive_signals_raise_alerts():
    for te in ({"packet_loss": 0.2, "path": "p"},
               {"dns_test": "failing"}, {"tls_error": True}):
        r = thousandeyes_responses(te)
        assert r["list_alerts"]["alerts"], f"expected an active alert for {te}"
        assert r["get_test_results"]["results"], f"expected results for {te}"


def test_thousandeyes_response_shape_matches_adapter_contract():
    r = thousandeyes_responses({"packet_loss": 0.2})
    assert set(r) == {"list_alerts", "get_test_results", "list_tests"}
    assert "alerts" in r["list_alerts"]
    assert "results" in r["get_test_results"] and "type" in r["get_test_results"]
    assert "tests" in r["list_tests"]


# --- folding removed / reachability honest ---------------------------------

def test_no_folding_in_provenance():
    for tid in ("EFIC-CERT-001", "EFIC-DNS-001", "EFIC-IAM-001"):
        prov = render(_entry(tid)["task"]).provenance
        assert prov["folded_to_logs"] == []


def test_unreachable_source_not_injected_into_splunk_logs():
    """A certificates payload must NOT appear as a Splunk log line (no folding)."""
    ch = render(_entry("EFIC-CERT-001")["task"]).channels
    blob = json.dumps(ch["splunk.search_logs"])
    assert "[certificates]" not in blob
    assert "cn=billing-gateway" not in blob


def test_cmdb_routed_to_servicenow_ci_not_folded():
    """CMDB evidence appears in ServiceNow CI (its production equivalent), never
    folded into Splunk."""
    ch = render(_entry("EFIC-CONFIG-DRIFT-001")["task"]).channels
    ci = json.dumps(ch["servicenow.get_ci_details"])
    assert "config_version" in ci
    assert "config_version" not in json.dumps(ch["splunk.search_logs"])


def test_engine_unreachable_mcps_documented():
    for mcp in ("certificates", "route53_dns", "identity", "aws_cloudwatch",
                "autosys"):
        assert mcp in ENGINE_UNREACHABLE
        assert mcp in ENGINE_UNREACHABLE_MCPS


def test_unreachable_sources_reported_in_provenance():
    prov = render(_entry("EFIC-IAM-001")["task"]).provenance
    # IAM scenario uses identity + aws_cloudwatch, both engine-unreachable.
    assert "identity" in prov["engine_unreachable"]
    assert "aws_cloudwatch" in prov["engine_unreachable"]
