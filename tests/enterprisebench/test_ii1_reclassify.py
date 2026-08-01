"""II-1 — evidence-driven reclassification tests.

The engine should trust an unambiguous decisive-evidence signal (an OOMKill) over
a summary-based misclassification, so the correct analyzer runs. Flag-gated; off ⇒
byte-identical.
"""
from __future__ import annotations

import pytest


def test_ii_reclassify_flag_default_off(monkeypatch):
    monkeypatch.delenv("II_RECLASSIFY_ENABLED", raising=False)
    from supervisor.agent import _ii_reclassify_enabled
    assert _ii_reclassify_enabled() is False


@pytest.fixture
def supervisor():
    from supervisor.agent import SentinalAISupervisor
    return SentinalAISupervisor()


def test_reclassify_oomkill_from_events(supervisor):
    events = [{"message": "OOMKilled :: reason=OOMKilled restarts=9"}]
    # a misclassified "error_spike" alert whose pods were actually OOMKilled
    assert supervisor._reclassify_from_evidence("error_spike", [], events) == "oomkill"


def test_reclassify_noop_without_signal(supervisor):
    logs = [{"message": "connection refused to db"}]
    assert supervisor._reclassify_from_evidence("error_spike", logs, []) == "error_spike"


def test_reclassify_noop_when_already_oomkill(supervisor):
    events = [{"message": "OOMKilled"}]
    assert supervisor._reclassify_from_evidence("oomkill", [], events) == "oomkill"


@pytest.mark.timeout(300)
def test_reclassify_fixes_oom_scenario_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rca(flags):
        for k in ("IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED",
                  "II_RECLASSIFY_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        for k in flags:
            monkeypatch.setenv(k, "true")
        s = run_corpus(only=["EFIC-K8S-OOM-001"])["scenarios"][0]
        return s["eic_dimensions"]["rca_correctness"]

    # IE domains alone do not fix a k8s OOM (no k8s domain); reclassify does.
    assert rca(["IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED"]) == 0.0
    assert rca(["IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED",
                "II_RECLASSIFY_ENABLED"]) == 1.0
