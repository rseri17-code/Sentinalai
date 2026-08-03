"""Phase 6 — Domain Intelligence Engine: Kubernetes module tests.

Second module on the SAME framework (no framework change). Interprets universal
Kubernetes failure signatures. Flag-gated (DI_KUBERNETES_ENABLED); off ⇒ byte-
identical; composes with the Database module (multiple domains active at once).
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import EvidenceView, enabled_modules
from supervisor.domain_intelligence.kubernetes import KubernetesIntelligence


def _view(logs=None, metrics=None, events=None, service="svc"):
    return EvidenceView(service, logs or [], metrics or {}, events or [], [])


def test_kubernetes_module_disabled_by_default(monkeypatch):
    for k in ("DI_DATABASE_ENABLED", "DI_KUBERNETES_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert enabled_modules() == []


def test_crashloop_and_imagepull_signatures():
    m = KubernetesIntelligence()
    clb = m.analyze(_view(events=[{"message": "CrashLoopBackOff :: reason=CrashLoopBackOff"}],
                          logs=[{"message": "FATAL missing config key OIDC_URL"}]))
    h = next(x for x in clb if x["name"] == "kubernetes_crashloopbackoff")
    for kw in ("config", "crashloop", "startup"):
        assert kw in h["root_cause"].lower()
    img = m.analyze(_view(events=[{"message": "ImagePullBackOff :: manifest for v9 not found"}]))
    assert any(x["name"] == "kubernetes_imagepullbackoff" for x in img)


def test_node_pressure_and_readiness_signatures():
    m = KubernetesIntelligence()
    ev = m.analyze(_view(events=[{"message": "Evicted :: node_condition=DiskPressure"}]))
    h = next(x for x in ev if x["name"] == "kubernetes_node_pressure_eviction")
    for kw in ("node pressure", "eviction", "ephemeral", "disk"):
        assert kw in h["root_cause"].lower()
    rd = m.analyze(_view(events=[{"message": "readiness probe failing :: readiness=failing"}]))
    assert any(x["name"] == "kubernetes_readiness_probe_failure" for x in rd)


def test_silent_on_clean_evidence():
    assert KubernetesIntelligence().analyze(
        _view(events=[{"message": "pod Running"}])) == []


def test_both_modules_register_without_framework_change(monkeypatch):
    monkeypatch.setenv("DI_DATABASE_ENABLED", "true")
    monkeypatch.setenv("DI_KUBERNETES_ENABLED", "true")
    names = {m.name for m in enabled_modules()}
    assert {"database", "kubernetes"} <= names


@pytest.mark.timeout(300)
def test_kubernetes_scenarios_fixed_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rca(only, flags):
        for k in ("IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED",
                  "II_RECLASSIFY_ENABLED", "DI_DATABASE_ENABLED",
                  "DI_KUBERNETES_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        for k in flags:
            monkeypatch.setenv(k, "true")
        return run_corpus(only=[only])["scenarios"][0]["eic_dimensions"]["rca_correctness"]

    assert rca("EFIC-K8S-CLB-001", []) == 0.0
    assert rca("EFIC-K8S-CLB-001", ["DI_KUBERNETES_ENABLED"]) == 1.0


@pytest.mark.timeout(300)
def test_adding_kubernetes_does_not_change_database(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rc(only, flags):
        for k in ("DI_DATABASE_ENABLED", "DI_KUBERNETES_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        for k in flags:
            monkeypatch.setenv(k, "true")
        return run_corpus(only=[only])["scenarios"][0]["engine_root_cause"]

    db_alone = rc("EFIC-DB-POOL-001", ["DI_DATABASE_ENABLED"])
    db_with_k8s = rc("EFIC-DB-POOL-001", ["DI_DATABASE_ENABLED", "DI_KUBERNETES_ENABLED"])
    assert db_alone == db_with_k8s        # Kubernetes module does not disturb Database
