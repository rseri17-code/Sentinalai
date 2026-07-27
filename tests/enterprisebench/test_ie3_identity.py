"""IE-3 — Identity/IAM vertical slice tests.

Proves the pilot: the full identity pipeline (acquisition → reasoning) works when
IE_IDENTITY_ENABLED, distinguishes authentication (expired signing key) from
authorization (revoked IAM permission), is inert when off (flag-off byte-
identical), and improves ONLY identity investigations.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# Flag-off inertness
# --------------------------------------------------------------------------- #

def test_identity_worker_inert_when_flag_off(monkeypatch):
    monkeypatch.setenv("IE_IDENTITY_ENABLED", "false")
    from workers.identity_worker import IdentityWorker
    w = IdentityWorker()
    assert w.execute("check_token_signing", {"service": "x"}) == {}
    assert w.execute("get_policy_changes", {"service": "x"}) == {}


def test_ie_identity_flag_default_off(monkeypatch):
    monkeypatch.delenv("IE_IDENTITY_ENABLED", raising=False)
    from supervisor.agent import _ie_identity_enabled
    assert _ie_identity_enabled() is False


def test_identity_stub_returns_production_empty():
    from workers.mcp_client import _stub_response
    assert _stub_response("identity.check_token_signing", "check_token_signing", {}) \
        == {"signing_key": None}
    assert _stub_response("identity.get_policy_changes", "get_policy_changes", {}) \
        == {"policy_changes": []}


def test_render_flag_off_keeps_identity_unreachable(monkeypatch):
    monkeypatch.setenv("IE_IDENTITY_ENABLED", "false")
    from enterprisebench.pipeline.render import render
    task = {"task_id": "T", "incident": {"service": "auth", "summary": "x"},
            "telemetry": {"identity": {"error": "signing key expired", "kid": "k1"}}}
    r = render(task)
    assert "identity.check_token_signing" not in r.channels
    assert "identity" in r.provenance["engine_unreachable"]


def test_render_flag_on_serves_identity(monkeypatch):
    monkeypatch.setenv("IE_IDENTITY_ENABLED", "true")
    from enterprisebench.pipeline.render import render
    task = {"task_id": "T", "incident": {"service": "auth", "summary": "x"},
            "telemetry": {"identity": {"error": "signing key expired", "kid": "k1"}}}
    r = render(task)
    assert r.channels["identity.check_token_signing"]["signing_key"]["status"] == "expired"
    assert "identity" in r.provenance["native_channel"]


# --------------------------------------------------------------------------- #
# Identity reasoning (unit) — authN vs authZ
# --------------------------------------------------------------------------- #

@pytest.fixture
def supervisor():
    from supervisor.agent import SentinalAISupervisor
    return SentinalAISupervisor()


def test_analyze_identity_authentication(supervisor):
    ev = {"signing_key": {"kid": "k-2024", "status": "expired"}}
    hyps = supervisor._analyze_identity("auth-service", ev)
    h = next(h for h in hyps if h.name == "signing_key_expiry")
    for kw in ("expired", "jwt", "signing key", "token"):
        assert kw in h.root_cause.lower()
    assert h.evidence_refs


def test_analyze_identity_authorization(supervisor):
    ev = {"policy_changes": [{"change": "CHG8 remove s3:GetObject", "effect": "deny"}]}
    hyps = supervisor._analyze_identity("reporting-batch", ev)
    h = next(h for h in hyps if h.name == "iam_permission_revoked")
    for kw in ("iam", "permission", "denied"):
        assert kw in h.root_cause.lower()


def test_analyze_identity_silent_on_clean(supervisor):
    assert supervisor._analyze_identity("svc", {}) == []
    clean = {"signing_key": {"kid": "k1", "status": "healthy"}, "policy_changes": []}
    assert supervisor._analyze_identity("svc", clean) == []


def test_identity_probe_gate(supervisor):
    auth_logs = {"logs": {"results": [{"message": "JWT signature validation failed"}]}}
    assert supervisor._identity_probe_warranted(auth_logs) is True
    authz_logs = {"logs": {"results": [{"message": "AccessDenied: not authorized"}]}}
    assert supervisor._identity_probe_warranted(authz_logs) is True
    other = {"logs": {"results": [{"message": "OOMKilled: pod restarted"}]}}
    assert supervisor._identity_probe_warranted(other) is False


def test_identity_worker_not_registered_when_flag_off(monkeypatch):
    monkeypatch.setenv("IE_IDENTITY_ENABLED", "false")
    from supervisor.agent import SentinalAISupervisor
    sup = SentinalAISupervisor()
    assert "identity_worker" not in sup.workers


# --------------------------------------------------------------------------- #
# End-to-end EB-2
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(240)
def test_flag_on_fixes_identity_scenarios(monkeypatch):
    monkeypatch.setenv("IE_IDENTITY_ENABLED", "true")
    from enterprisebench.pipeline.run import run_corpus
    r = run_corpus(only=["EFIC-IAM-001", "EFIC-IDENTITY-001"])
    by = {s["scenario_id"]: s for s in r["scenarios"]}
    for sid in ("EFIC-IAM-001", "EFIC-IDENTITY-001"):
        s = by[sid]
        assert s["eic_dimensions"]["rca_correctness"] == 1.0, f"{sid} RCA not fixed"
        assert "identity" in s["servers_queried"], f"{sid} did not query identity"
        assert s["process"]["confidence_in_expected_range"]["raw"] == 1.0


@pytest.mark.timeout(240)
def test_flag_off_leaves_identity_scenarios_unfixed(monkeypatch):
    monkeypatch.delenv("IE_IDENTITY_ENABLED", raising=False)
    from enterprisebench.pipeline.run import run_corpus
    r = run_corpus(only=["EFIC-IAM-001"])
    s = r["scenarios"][0]
    assert s["eic_dimensions"]["rca_correctness"] == 0.0
    assert "identity" not in s["servers_queried"]
