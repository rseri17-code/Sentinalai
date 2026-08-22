"""Phase 6 — Domain Intelligence Engine: Application Runtime module tests.

Fourth module on the unchanged framework. Interprets thread-pool exhaustion and
stop-the-world GC pauses. Flag-gated (DI_APPLICATION_RUNTIME_ENABLED); off ⇒
byte-identical.
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import EvidenceView, enabled_modules
from supervisor.domain_intelligence.application_runtime import (
    ApplicationRuntimeIntelligence)


def _view(logs=None, metrics=None, events=None, service="svc"):
    return EvidenceView(service, logs or [], metrics or {}, events or [], [])


def test_module_disabled_by_default(monkeypatch):
    for k in ("DI_DATABASE_ENABLED", "DI_APPLICATION_RUNTIME_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert enabled_modules() == []


def test_thread_pool_exhaustion_signature():
    h = ApplicationRuntimeIntelligence().analyze(
        _view(logs=[{"message": "thread pool exhausted; task rejected"}]))
    x = next(y for y in h if y["name"] == "thread_pool_exhaustion")
    for kw in ("thread pool", "exhaustion", "blocked", "starv"):
        assert kw in x["root_cause"].lower()


def test_gc_pause_signature_from_metric():
    h = ApplicationRuntimeIntelligence().analyze(
        _view(metrics={"metrics": [{"name": "sysdig_cpu_spikes", "value": "aligned to GC"}]}))
    x = next(y for y in h if y["name"] == "gc_pause_latency")
    for kw in ("gc", "heap", "pause", "stop-the-world"):
        assert kw in x["root_cause"].lower()


def test_silent_on_clean_evidence():
    assert ApplicationRuntimeIntelligence().analyze(
        _view(logs=[{"message": "request 200 OK"}])) == []


@pytest.mark.timeout(300)
def test_app_scenarios_fixed_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rca(only, on):
        monkeypatch.delenv("DI_APPLICATION_RUNTIME_ENABLED", raising=False)
        if on:
            monkeypatch.setenv("DI_APPLICATION_RUNTIME_ENABLED", "true")
        return run_corpus(only=[only])["scenarios"][0]["eic_dimensions"]["rca_correctness"]

    for sid in ("EFIC-APP-GC-001", "EFIC-APP-THREADPOOL-001"):
        assert rca(sid, on=False) == 0.0
        assert rca(sid, on=True) == 1.0
