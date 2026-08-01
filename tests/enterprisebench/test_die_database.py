"""Phase 6 — Domain Intelligence Engine: Database module tests.

The Database Intelligence Module interprets universal DB failure signatures
(deadlock, pool exhaustion, query-plan regression, replica lag) into canonical
root-cause hypotheses. Flag-gated (DI_DATABASE_ENABLED); off ⇒ byte-identical.
"""
from __future__ import annotations

import pytest

from supervisor.domain_intelligence import (EvidenceView, enabled_modules,
                                            run_domain_modules)
from supervisor.domain_intelligence.database import DatabaseIntelligence


def _view(logs=None, metrics=None, events=None, service="svc"):
    return EvidenceView(service, logs or [], metrics or {}, events or [], [])


def test_modules_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DI_DATABASE_ENABLED", raising=False)
    assert enabled_modules() == []
    assert run_domain_modules(_view()) == []


def test_deadlock_signature():
    m = DatabaseIntelligence()
    hyps = m.analyze(_view(logs=[{"message": "deadlock victim; transaction rolled back"}]))
    h = next(h for h in hyps if h["name"] == "database_deadlock")
    for kw in ("deadlock", "lock", "database"):
        assert kw in h["root_cause"].lower()
    assert h["recommendation"]


def test_pool_exhaustion_from_log_and_from_metrics():
    m = DatabaseIntelligence()
    # from a log signature
    h = m.analyze(_view(logs=[{"message": "HikariPool-1 connection is not available, timeout"}]))
    assert any(x["name"] == "database_pool_exhaustion" for x in h)
    # from active==max metrics alone
    h = m.analyze(_view(metrics={"metrics": [{"name": "database_active", "value": 200},
                                             {"name": "database_max", "value": 200}]}))
    assert any(x["name"] == "database_pool_exhaustion" for x in h)


def test_slow_query_and_replica_lag_signatures():
    m = DatabaseIntelligence()
    sq = m.analyze(_view(metrics={"metrics": [{"name": "database_plan",
                                              "value": "seq scan on accounts"}]}))
    h = next(x for x in sq if x["name"] == "database_slow_query")
    assert "query plan" in h["root_cause"].lower() and "scan" in h["root_cause"].lower()
    rl = m.analyze(_view(logs=[{"message": "read-after-write mismatch"}]))
    assert any(x["name"] == "database_replica_lag" for x in rl)


def test_silent_on_clean_evidence():
    assert DatabaseIntelligence().analyze(
        _view(logs=[{"message": "request completed 200 OK"}])) == []


def test_evidence_view_text_union():
    v = _view(logs=[{"message": "AAA"}],
              metrics={"metrics": [{"name": "n", "value": "BBB"}]},
              events=[{"message": "CCC"}])
    assert "aaa" in v.text and "n=bbb" in v.text and "ccc" in v.text


@pytest.mark.timeout(300)
def test_db_module_fixes_db_scenarios_only_when_on(monkeypatch):
    from enterprisebench.pipeline.run import run_corpus

    def rca(only, flags):
        for k in ("IE_DNS_ENABLED", "IE_IDENTITY_ENABLED", "IE_AWS_ENABLED",
                  "II_RECLASSIFY_ENABLED", "DI_DATABASE_ENABLED"):
            monkeypatch.delenv(k, raising=False)
        for k in flags:
            monkeypatch.setenv(k, "true")
        return run_corpus(only=[only])["scenarios"][0]["eic_dimensions"]["rca_correctness"]

    assert rca("EFIC-DB-POOL-001", []) == 0.0
    assert rca("EFIC-DB-POOL-001", ["DI_DATABASE_ENABLED"]) == 1.0
