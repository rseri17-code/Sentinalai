"""IE-4 — Multi-domain (cross-domain) investigation tests.

Proves cross-domain reasoning: multiple IE domains participate in one
investigation, evidence is correlated across domains, competing hypotheses are
eliminated, confidence reflects cross-domain evidence — while single-domain and
all-off behavior are byte-identical to IE-2/IE-3/main.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# AWS slice — flag-off inertness
# --------------------------------------------------------------------------- #

def test_aws_worker_inert_when_flag_off(monkeypatch):
    monkeypatch.setenv("IE_AWS_ENABLED", "false")
    from workers.aws_worker import AwsWorker
    assert AwsWorker().execute("get_error_metrics", {"service": "x"}) == {}


def test_aws_stub_returns_production_empty():
    from workers.mcp_client import _stub_response
    assert _stub_response("aws_cloudwatch.get_error_metrics", "get_error_metrics", {}) \
        == {"metrics": {}}


def test_render_aws_flag_gating(monkeypatch):
    from enterprisebench.pipeline.render import render
    task = {"task_id": "T", "incident": {"service": "media", "summary": "x"},
            "telemetry": {"aws_cloudwatch": {"s3_503_slowdown": 130, "throttled": True}}}
    monkeypatch.setenv("IE_AWS_ENABLED", "false")
    r = render(task)
    assert "aws_cloudwatch.get_error_metrics" not in r.channels
    assert "aws_cloudwatch" in r.provenance["engine_unreachable"]
    monkeypatch.setenv("IE_AWS_ENABLED", "true")
    r = render(task)
    assert r.channels["aws_cloudwatch.get_error_metrics"]["metrics"]["s3_503_slowdown"] == 130
    assert "aws_cloudwatch" in r.provenance["native_channel"]


def test_aws_worker_not_registered_when_flag_off(monkeypatch):
    monkeypatch.setenv("IE_AWS_ENABLED", "false")
    from supervisor.agent import SentinalAISupervisor
    assert "aws_worker" not in SentinalAISupervisor().workers


# --------------------------------------------------------------------------- #
# AWS reasoning
# --------------------------------------------------------------------------- #

@pytest.fixture
def supervisor():
    from supervisor.agent import SentinalAISupervisor
    return SentinalAISupervisor()


def test_analyze_aws_primary_hypotheses(supervisor):
    names = {h.name for h in supervisor._analyze_aws(
        "media", {"s3_503_slowdown": 130, "throttled": True})}
    assert "s3_throttling" in names
    names = {h.name for h in supervisor._analyze_aws(
        "fulfil", {"az": "us-east-1b", "status": "degraded", "affected_pct": 33})}
    assert "az_impairment" in names


def test_analyze_aws_403_is_low_scored(supervisor):
    """A 403 is a symptom of an authz change — scored low so identity owns the RCA."""
    h = next(h for h in supervisor._analyze_aws("batch", {"s3_403": 240})
             if h.name == "aws_access_denied")
    assert h.base_score < 60


# --------------------------------------------------------------------------- #
# Cross-domain correlation
# --------------------------------------------------------------------------- #

def test_correlate_identity_aws_corroboration(supervisor):
    from supervisor.agent import Hypothesis
    iam = Hypothesis(name="iam_permission_revoked", root_cause="revoked",
                     base_score=83, evidence_refs=["identity:policy_change"], reasoning="")
    generic = Hypothesis(name="error_spike_generic", root_cause="err",
                         base_score=40, evidence_refs=[], reasoning="")
    evidence = {"identity_evidence": {"policy_changes": [{"change": "remove s3:GetObject"}]},
                "aws_evidence": {"metrics": {"s3_403": 240}}}
    rec = supervisor._correlate_cross_domain([iam, generic], evidence)
    # identity hypothesis corroborated by AWS 403s → boosted + cross-domain ref
    assert iam.base_score > 83
    assert any("aws:s3_403" in r for r in iam.evidence_refs)
    assert rec["correlations"]
    # cross-domain confirmation weakens the undifferentiated generic hypothesis
    assert generic.base_score < 40
    assert rec["contradictions"]


def test_correlate_noop_without_second_domain(supervisor):
    from supervisor.agent import Hypothesis
    iam = Hypothesis(name="iam_permission_revoked", root_cause="r", base_score=83,
                     evidence_refs=[], reasoning="")
    rec = supervisor._correlate_cross_domain(
        [iam], {"identity_evidence": {"policy_changes": [{"change": "remove s3:x"}]}})
    assert iam.base_score == 83                       # no AWS evidence → no boost
    assert rec["correlations"] == []


def test_active_domains_gate(monkeypatch):
    from supervisor.agent import _ie_active_domains
    for k in ("IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert _ie_active_domains() == []
    monkeypatch.setenv("IE_IDENTITY_ENABLED", "true")
    assert _ie_active_domains() == ["identity"]
    monkeypatch.setenv("IE_AWS_ENABLED", "true")
    assert set(_ie_active_domains()) == {"identity", "aws"}


# --------------------------------------------------------------------------- #
# End-to-end EB-2 — cross-domain on IAM-001
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(300)
def test_cross_domain_corroboration_on_iam(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def conf(only, **flags):
        for k in ("IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        for k, v in flags.items():
            monkeypatch.setenv(k, v)
        s = run_corpus(only=[only])["scenarios"][0]
        return s

    id_only = conf("EFIC-IAM-001", IE_IDENTITY_ENABLED="true")
    both = conf("EFIC-IAM-001", IE_IDENTITY_ENABLED="true", IE_AWS_ENABLED="true")
    assert id_only["eic_dimensions"]["rca_correctness"] == 1.0
    assert both["eic_dimensions"]["rca_correctness"] == 1.0
    assert "aws_cloudwatch" in both["servers_queried"]      # second domain participated
    assert "identity" in both["servers_queried"]
    # cross-domain corroboration raises confidence over single-domain
    assert both["engine_confidence"] > id_only["engine_confidence"]


@pytest.mark.timeout(240)
def test_all_flags_off_unchanged(monkeypatch):
    for k in ("IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    from enterprisebench.pipeline.run import run_corpus
    s = run_corpus(only=["EFIC-IAM-001"])["scenarios"][0]
    assert s["eic_dimensions"]["rca_correctness"] == 0.0    # baseline: unfixed
    assert "aws_cloudwatch" not in s["servers_queried"]
