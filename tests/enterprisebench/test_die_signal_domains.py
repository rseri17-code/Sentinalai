"""Phase 6 — DIE: domains that read the APM (dynatrace) signal detail.

EvidenceView.signals surfaces specific dynatrace content (redis_evicted_keys,
errors_by_az, ingest_lag) that the golden-signals summary drops. Redis-eviction,
observability blind-spot, and AZ-impairment reasoning consume it. Flag-gated;
off ⇒ byte-identical.
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import EvidenceView
from supervisor.domain_intelligence.messaging import MessagingIntelligence
from supervisor.domain_intelligence.observability import ObservabilityIntelligence
from supervisor.domain_intelligence.cloud import CloudIntelligence


def _v(logs=None, signals=None, service="svc"):
    return EvidenceView(service, logs or [], {}, [], [], signals=signals or {})


def _names(hyps):
    return {h["name"] for h in hyps}


def test_evidence_view_surfaces_signal_detail():
    v = _v(signals={"efic_context": "redis_evicted_keys=500000 cache_hit=0.99->0.4"})
    assert "redis_evicted_keys" in v.text and "cache_hit" in v.text


def test_redis_eviction_stampede_from_apm_signal():
    h = MessagingIntelligence().analyze(_v(
        signals={"efic_context": "cache_hit=0.99->0.4 db_qps=10x redis_evicted_keys=500000"}))
    x = next(y for y in h if y["name"] == "redis_eviction_stampede")
    for kw in ("redis", "eviction", "cache", "stampede"):
        assert kw in x["root_cause"].lower()


def test_observability_blind_spot_needs_stall_and_real_errors():
    m = ObservabilityIntelligence()
    hit = m.analyze(_v(logs=[{"message": "wallet debit failed"}],
                       signals={"efic_context": "error_rate=0% (metrics ingestion stalled) ingest_lag=45m"}))
    x = next(y for y in hit if y["name"] == "observability_blind_spot")
    for kw in ("blind spot", "gap", "metrics"):
        assert kw in x["root_cause"].lower()
    # a metrics stall with NO real errors logged => not a blind spot
    assert m.analyze(_v(signals={"efic_context": "ingest_lag=45m"})) == []


def test_cloud_az_impairment_isolated_to_one_zone():
    h = CloudIntelligence().analyze(_v(
        signals={"efic_context": "errors_by_az={1a=normal, 1b=high}"}))
    x = next(y for y in h if y["name"] == "aws_az_impairment")
    for kw in ("availability zone", "az", "impairment", "regional"):
        assert kw in x["root_cause"].lower()
    # errors spread across AZs (none singled out) => not an AZ impairment
    assert CloudIntelligence().analyze(_v(logs=[{"message": "generic error"}])) == []


@pytest.mark.timeout(360)
def test_final_three_fixed_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus
    cases = {
        "EFIC-REDIS-EVICT-001": "DI_MESSAGING_ENABLED",
        "EFIC-OBS-BLINDSPOT-001": "DI_OBSERVABILITY_ENABLED",
        "EFIC-AWS-REGION-001": "DI_CLOUD_ENABLED",
    }
    for sid, flag in cases.items():
        monkeypatch.delenv(flag, raising=False)
        off = run_corpus(only=[sid])["scenarios"][0]["eic_dimensions"]["rca_correctness"]
        monkeypatch.setenv(flag, "true")
        on = run_corpus(only=[sid])["scenarios"][0]["eic_dimensions"]["rca_correctness"]
        monkeypatch.delenv(flag, raising=False)
        assert off == 0.0 and on == 1.0, f"{sid}: off={off} on={on}"
