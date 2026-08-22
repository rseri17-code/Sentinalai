"""Phase 6 — Domain Intelligence Engine: Deployment module tests.

Fifth module on the unchanged framework. Change-correlation reasoning: an exception
after a release => regression; inconsistent config across nodes => drift. Flag-gated
(DI_DEPLOYMENT_ENABLED); off ⇒ byte-identical.
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import EvidenceView, enabled_modules
from supervisor.domain_intelligence.deployment import DeploymentIntelligence


def _view(logs=None, changes=None, service="checkout-service"):
    v = EvidenceView(service, logs or [], {}, [], changes or [])
    return v


def test_module_disabled_by_default(monkeypatch):
    for k in ("DI_DATABASE_ENABLED", "DI_DEPLOYMENT_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert enabled_modules() == []


def test_release_regression_needs_error_and_release_change():
    m = DeploymentIntelligence()
    # exception + a release change => regression (with version)
    h = m.analyze(_view(logs=[{"message": "NullPointerException in v8.4"}],
                        changes=[{"short_description": "CHG9 v8.4 release"}]))
    x = next(y for y in h if y["name"] == "release_regression")
    for kw in ("regression", "release", "deployment", "v8.4"):
        assert kw in x["root_cause"].lower()
    # exception with NO release change => no regression hypothesis
    h2 = m.analyze(_view(logs=[{"message": "NullPointerException"}], changes=[]))
    assert not any(y["name"] == "release_regression" for y in h2)


def test_config_drift_signature():
    h = DeploymentIntelligence().analyze(
        _view(logs=[{"message": "only some nodes: invalid index config"}],
              changes=[{"short_description": "nodeA=v3 nodeB=v2"}]))
    x = next(y for y in h if y["name"] == "config_drift")
    for kw in ("config", "drift", "inconsistent", "nodes"):
        assert kw in x["root_cause"].lower()


def test_regression_and_drift_are_mutually_exclusive():
    m = DeploymentIntelligence()
    reg = {y["name"] for y in m.analyze(
        _view(logs=[{"message": "NullPointerException in v8.4"}],
              changes=[{"short_description": "CHG9 v8.4 release"}]))}
    assert reg == {"release_regression"}
    drift = {y["name"] for y in m.analyze(
        _view(logs=[{"message": "only some nodes: invalid index config"}],
              changes=[{"short_description": "nodeA=v3 nodeB=v2"}]))}
    assert drift == {"config_drift"}


def test_silent_on_clean_evidence():
    assert DeploymentIntelligence().analyze(_view(logs=[{"message": "200 OK"}])) == []


@pytest.mark.timeout(300)
def test_deployment_scenarios_fixed_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rca(only, on):
        monkeypatch.delenv("DI_DEPLOYMENT_ENABLED", raising=False)
        if on:
            monkeypatch.setenv("DI_DEPLOYMENT_ENABLED", "true")
        return run_corpus(only=[only])["scenarios"][0]["eic_dimensions"]["rca_correctness"]

    for sid in ("EFIC-DEPLOY-001", "EFIC-CONFIG-DRIFT-001"):
        assert rca(sid, on=False) == 0.0
        assert rca(sid, on=True) == 1.0
