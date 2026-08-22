"""Phase 6 — Domain Intelligence Engine: batch of reachable single-mode domains.

Messaging (Kafka), Certificates, Batch, Storage, Network modules + the Database
cascade rule — each on the unchanged framework, flag-gated, off ⇒ byte-identical.
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import EvidenceView
from supervisor.domain_intelligence.messaging import MessagingIntelligence
from supervisor.domain_intelligence.certificates import CertificateIntelligence
from supervisor.domain_intelligence.batch import BatchIntelligence
from supervisor.domain_intelligence.storage import StorageIntelligence
from supervisor.domain_intelligence.network import NetworkIntelligence
from supervisor.domain_intelligence.database import DatabaseIntelligence


def _v(logs=None, metrics=None, changes=None, service="svc"):
    return EvidenceView(service, logs or [], metrics or {}, [], changes or [])


def _names(hyps):
    return {h["name"] for h in hyps}


def test_kafka_consumer_lag():
    h = MessagingIntelligence().analyze(
        _v(logs=[{"message": "deserialization error; consumer paused"}]))
    x = next(y for y in h if y["name"] == "kafka_consumer_lag_poison")
    for kw in ("consumer", "kafka", "lag", "poison"):
        assert kw in x["root_cause"].lower()


def test_certificate_expiry():
    h = CertificateIntelligence().analyze(
        _v(logs=[{"message": "tls handshake failure: certificate expired"}]))
    x = next(y for y in h if y["name"] == "certificate_expiry")
    for kw in ("certificate", "expired", "handshake", "tls"):
        assert kw in x["root_cause"].lower()


def test_batch_upstream_dependency():
    h = BatchIntelligence().analyze(_v(logs=[{"message": "upstream file not found"}]))
    x = next(y for y in h if y["name"] == "batch_upstream_dependency_failure")
    for kw in ("job", "terminated", "upstream file"):
        assert kw in x["root_cause"].lower()


def test_storage_volume_full():
    h = StorageIntelligence().analyze(
        _v(logs=[{"message": "ENOSPC: no space left on device"}],
           metrics={"metrics": [{"name": "sysdig_volume_pct", "value": 100}]}))
    x = next(y for y in h if y["name"] == "storage_volume_full")
    for kw in ("no space", "volume"):
        assert kw in x["root_cause"].lower()


def test_network_security_group_and_packet_loss():
    m = NetworkIntelligence()
    sg = m.analyze(_v(logs=[{"message": "connection timeout to db:5432"}],
                      changes=[{"short_description": "CHG95 SG edit remove 5432 ingress"}]))
    x = next(y for y in sg if y["name"] == "security_group_ingress_block")
    for kw in ("security group", "ingress", "blocked"):
        assert kw in x["root_cause"].lower()
    # 504 + healthy backend => packet loss (negative evidence)
    pl = m.analyze(_v(logs=[{"message": "upstream timeout 504"}],
                      metrics={"metrics": [{"name": "database_active", "value": 20},
                                           {"name": "database_max", "value": 200}]}))
    assert "network_packet_loss" in _names(pl)
    # 504 with a saturated backend => NOT attributed to packet loss
    busy = m.analyze(_v(logs=[{"message": "upstream timeout 504"}],
                        metrics={"metrics": [{"name": "database_active", "value": 200},
                                             {"name": "database_max", "value": 200}]}))
    assert "network_packet_loss" not in _names(busy)


def test_database_cascade_vs_pool():
    m = DatabaseIntelligence()
    # near-max + a service timeout + downstream 5xx => cascade naming the origin
    casc = m.analyze(_v(logs=[{"message": "inventory timeout"}, {"message": "cart 503"}],
                        metrics={"metrics": [{"name": "database_active", "value": 199},
                                             {"name": "database_max", "value": 200}]}))
    x = next(y for y in casc if y["name"] == "database_saturation_cascade")
    for kw in ("cascad", "inventory", "saturation"):
        assert kw in x["root_cause"].lower()
    # pool timeout WITHOUT a downstream 5xx => pool exhaustion, not cascade
    pool = m.analyze(_v(logs=[{"message": "HikariPool timeout"}],
                        metrics={"metrics": [{"name": "database_active", "value": 200},
                                             {"name": "database_max", "value": 200}]}))
    assert "database_saturation_cascade" not in _names(pool)
    assert "database_pool_exhaustion" in _names(pool)


@pytest.mark.timeout(360)
def test_batch_domains_fix_reachable_scenarios(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus
    flags = {
        "EFIC-KAFKA-LAG-001": "DI_MESSAGING_ENABLED",
        "EFIC-CERT-001": "DI_CERTIFICATES_ENABLED",
        "EFIC-BATCH-AUTOSYS-001": "DI_BATCH_ENABLED",
        "EFIC-STORAGE-VOLFULL-001": "DI_STORAGE_ENABLED",
        "EFIC-NET-LOSS-001": "DI_NETWORK_ENABLED",
        "EFIC-AWS-SG-001": "DI_NETWORK_ENABLED",
        "EFIC-CASCADE-001": "DI_DATABASE_ENABLED",
    }
    for sid, flag in flags.items():
        monkeypatch.delenv(flag, raising=False)
        off = run_corpus(only=[sid])["scenarios"][0]["eic_dimensions"]["rca_correctness"]
        monkeypatch.setenv(flag, "true")
        on = run_corpus(only=[sid])["scenarios"][0]["eic_dimensions"]["rca_correctness"]
        monkeypatch.delenv(flag, raising=False)
        assert off == 0.0 and on == 1.0, f"{sid}: off={off} on={on}"
