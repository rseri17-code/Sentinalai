"""Phase 6 — Domain Intelligence Engine: API Gateway module tests.

Third module on the SAME framework (no framework change). Distinguishes cause from
symptom: 429 + healthy backend => rate-limit saturation (not backend overload);
TLS handshake + istio cert-reload => mTLS/sidecar (not a network outage). Flag-gated
(DI_API_GATEWAY_ENABLED); off => byte-identical.
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import EvidenceView, enabled_modules
from supervisor.domain_intelligence.api_gateway import ApiGatewayIntelligence


def _view(logs=None, metrics=None, events=None, service="public-api"):
    return EvidenceView(service, logs or [], metrics or {}, events or [], [])


def test_module_disabled_by_default(monkeypatch):
    for k in ("DI_DATABASE_ENABLED", "DI_KUBERNETES_ENABLED", "DI_API_GATEWAY_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert enabled_modules() == []


def test_rate_limit_distinguishes_cause_from_symptom():
    m = ApiGatewayIntelligence()
    # 429 with a healthy backend (active well below max) => rate-limit bucket cause
    healthy = m.analyze(_view(
        logs=[{"message": "429 rate limit exceeded"}],
        metrics={"metrics": [{"name": "database_active", "value": 30},
                             {"name": "database_max", "value": 200}]}))
    h = next(x for x in healthy if x["name"] == "gateway_rate_limit_saturation")
    for kw in ("429", "rate limit", "quota"):
        assert kw in h["root_cause"].lower()
    assert "metrics:backend_healthy" in h["evidence_refs"]      # negative evidence used
    assert h["base_score"] == 82
    # 429 with a saturated backend => lower confidence (ambiguous vs backend overload)
    sat = m.analyze(_view(
        logs=[{"message": "429 too many requests"}],
        metrics={"metrics": [{"name": "database_active", "value": 200},
                             {"name": "database_max", "value": 200}]}))
    assert next(x for x in sat
                if x["name"] == "gateway_rate_limit_saturation")["base_score"] == 74


def test_istio_mtls_signature():
    m = ApiGatewayIntelligence()
    h = m.analyze(_view(
        logs=[{"message": "upstream connect error; TLS handshake"}],
        events=[{"message": "istio-proxy cert reload failed"}]))
    x = next(h_ for h_ in h if h_["name"] == "istio_mtls_failure")
    for kw in ("istio", "mtls", "sidecar", "certificate"):
        assert kw in x["root_cause"].lower()


def test_silent_on_clean_evidence():
    assert ApiGatewayIntelligence().analyze(
        _view(logs=[{"message": "200 OK"}])) == []


@pytest.mark.timeout(300)
def test_gateway_scenarios_fixed_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rca(only, on):
        for k in ("DI_API_GATEWAY_ENABLED",):
            monkeypatch.delenv(k, raising=False)
        if on:
            monkeypatch.setenv("DI_API_GATEWAY_ENABLED", "true")
        return run_corpus(only=[only])["scenarios"][0]["eic_dimensions"]["rca_correctness"]

    for sid in ("EFIC-GW-RATELIMIT-001", "EFIC-ISTIO-MTLS-001"):
        assert rca(sid, on=False) == 0.0
        assert rca(sid, on=True) == 1.0


@pytest.mark.timeout(300)
def test_gateway_does_not_change_prior_domains(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rc(only, flags):
        for k in ("DI_DATABASE_ENABLED", "DI_KUBERNETES_ENABLED",
                  "DI_API_GATEWAY_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        for k in flags:
            monkeypatch.setenv(k, "true")
        return run_corpus(only=[only])["scenarios"][0]["engine_root_cause"]

    db = rc("EFIC-DB-POOL-001", ["DI_DATABASE_ENABLED"])
    db2 = rc("EFIC-DB-POOL-001", ["DI_DATABASE_ENABLED", "DI_API_GATEWAY_ENABLED"])
    assert db == db2
