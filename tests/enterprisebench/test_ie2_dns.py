"""IE-2 — DNS/Route53 vertical slice tests.

Proves the pilot: the full 8-link DNS pipeline (acquisition → reasoning) works
when IE_DNS_ENABLED, is completely inert when off (flag-off byte-identical), and
improves ONLY DNS investigations. Fast unit tests + a couple of end-to-end EB-2
checks.
"""
from __future__ import annotations

import json
import os

import pytest


# --------------------------------------------------------------------------- #
# Flag-off inertness (defence in depth)
# --------------------------------------------------------------------------- #

def test_dns_worker_inert_when_flag_off(monkeypatch):
    monkeypatch.setenv("IE_DNS_ENABLED", "false")
    from workers.dns_worker import DnsWorker
    w = DnsWorker()
    assert w.execute("get_dns_record", {"service": "x"}) == {}
    assert w.execute("check_resolver", {"service": "x"}) == {}


def test_ie_dns_flag_default_off(monkeypatch):
    monkeypatch.delenv("IE_DNS_ENABLED", raising=False)
    from supervisor.agent import _ie_dns_enabled
    assert _ie_dns_enabled() is False


def test_route53_stub_returns_production_empty():
    from workers.mcp_client import _stub_response
    assert _stub_response("route53.get_record", "get_dns_record", {}) == {"record": None}
    assert _stub_response("route53.check_resolver", "check_resolver", {}) == {"resolver": None}


def test_render_flag_off_keeps_route53_unreachable(monkeypatch):
    monkeypatch.setenv("IE_DNS_ENABLED", "false")
    from enterprisebench.pipeline.render import render
    task = {"task_id": "T", "incident": {"service": "catalog", "summary": "x"},
            "telemetry": {"route53_dns": {"record": "db.catalog", "points_to": "old-endpoint"}}}
    r = render(task)
    assert "route53.get_record" not in r.channels
    assert "route53_dns" in r.provenance["engine_unreachable"]


def test_render_flag_on_serves_route53(monkeypatch):
    monkeypatch.setenv("IE_DNS_ENABLED", "true")
    from enterprisebench.pipeline.render import render
    task = {"task_id": "T", "incident": {"service": "catalog", "summary": "x"},
            "telemetry": {"route53_dns": {"record": "db.catalog", "points_to": "old-endpoint"}}}
    r = render(task)
    assert r.channels["route53.get_record"]["record"]["points_to"] == "old-endpoint"
    assert "route53_dns" in r.provenance["native_channel"]


# --------------------------------------------------------------------------- #
# DNS reasoning (unit)
# --------------------------------------------------------------------------- #

@pytest.fixture
def supervisor():
    from supervisor.agent import SentinalAISupervisor
    return SentinalAISupervisor()


def test_analyze_dns_emits_stale_record(supervisor):
    dns_ev = {"record": {"name": "db.catalog", "points_to": "old-endpoint"}}
    hyps = supervisor._analyze_dns("catalog-service", dns_ev, logs=[])
    names = {h.name for h in hyps}
    assert "stale_dns_record" in names
    h = next(h for h in hyps if h.name == "stale_dns_record")
    for kw in ("stale", "route53", "dns", "record"):
        assert kw in h.root_cause.lower()
    assert h.evidence_refs                       # cited → lifts the citation gate


def test_analyze_dns_emits_resolver_outage(supervisor):
    dns_ev = {"resolver": {"status": "unhealthy", "query_timeouts": "high"}}
    hyps = supervisor._analyze_dns("multiple", dns_ev, logs=[])
    h = next(h for h in hyps if h.name == "dns_resolver_outage")
    for kw in ("dns", "resolver", "nxdomain", "resolution"):
        assert kw in h.root_cause.lower()


def test_analyze_dns_silent_on_clean_evidence(supervisor):
    # Healthy resolver + record pointing to a live endpoint → no DNS hypothesis.
    assert supervisor._analyze_dns("svc", {}, logs=[]) == []
    clean = {"resolver": {"status": "healthy"},
             "record": {"name": "db", "points_to": "current-endpoint"}}
    assert supervisor._analyze_dns("svc", clean, logs=[]) == []


def test_dns_probe_gate(supervisor):
    dns_logs = {"logs": {"results": [{"message": "name resolution failed; NXDOMAIN"}]}}
    assert supervisor._dns_probe_warranted(dns_logs) is True
    other = {"logs": {"results": [{"message": "OOMKilled: pod restarted"}]}}
    assert supervisor._dns_probe_warranted(other) is False


def test_dns_worker_not_registered_when_flag_off(monkeypatch):
    monkeypatch.setenv("IE_DNS_ENABLED", "false")
    from supervisor.agent import SentinalAISupervisor
    sup = SentinalAISupervisor()
    assert "dns_worker" not in sup.workers


# --------------------------------------------------------------------------- #
# End-to-end EB-2 (subprocess; a few seconds)
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(240)
def test_flag_on_fixes_dns_scenarios(monkeypatch):
    monkeypatch.setenv("IE_DNS_ENABLED", "true")
    from enterprisebench.pipeline.run import run_corpus
    r = run_corpus(only=["EFIC-DNS-001", "EFIC-DNS-RESOLVER-001"])
    by = {s["scenario_id"]: s for s in r["scenarios"]}
    for sid in ("EFIC-DNS-001", "EFIC-DNS-RESOLVER-001"):
        s = by[sid]
        assert s["eic_dimensions"]["rca_correctness"] == 1.0, f"{sid} RCA not fixed"
        assert "route53" in s["servers_queried"], f"{sid} did not query route53"
        assert s["process"]["confidence_in_expected_range"]["raw"] == 1.0


@pytest.mark.timeout(240)
def test_flag_off_leaves_dns_scenarios_unfixed(monkeypatch):
    monkeypatch.delenv("IE_DNS_ENABLED", raising=False)
    from enterprisebench.pipeline.run import run_corpus
    r = run_corpus(only=["EFIC-DNS-001"])
    s = r["scenarios"][0]
    assert s["eic_dimensions"]["rca_correctness"] == 0.0     # unchanged from baseline
    assert "route53" not in s["servers_queried"]
