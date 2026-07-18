"""OIP Service #3 — Application Health. Produce-only composition tests.

Verifies the per-application rollup answers the application owner's questions
from existing investigation evidence and the two shipped OIP services
(operational_health, incident_trends), preserving evidence/confidence/
verifiability attribution, staying deterministic — with zero new intelligence
and no runtime touch.
"""
from __future__ import annotations

import json

from sentinel_core.oip import application_health


def _result(iid, svc, rc, itype="saturation", status="supports", ev_conf=78,
            complete=0.85, corpus="corpus:abc"):
    r = {
        "incident_id": iid, "root_cause": rc, "confidence": 80,
        "incident_type": itype,
        "_investigation_validation": {
            "root_cause_verification": {"verification_status": status},
            "evidence_validation": {"evidence_validation_score": 0.85},
            "confidence_reconstruction": {"evidence_confidence": ev_conf},
            "investigation_completeness": {
                "investigation_completeness_score": complete},
            "expert_concordance": {"independent_winner": rc}},
        "_causal_investigation": {"localization": {"root_cause_service": svc}},
        "_evidence_lifecycle": {"counts": {"used": 5, "filtered": 0,
                                            "unavailable": 0, "error": 0}},
    }
    if corpus:
        r["_corpus_version"] = corpus
    return r


def _inc(iid, app, svc, itype, period, owner=""):
    d = {"incident_id": iid, "application": app, "service": svc,
         "incident_type": itype, "created_at": period + "T00:00:00",
         "period": period}
    if owner:
        d["owner"] = owner
    return d


def _sample():
    """checkout (payments+cart+edge, edge degraded -> at_risk, db cause
    recurs 3x) and billing (healthy single service)."""
    rows = [
        ("INC-1", "checkout", "payments", "db pool exhaustion", "saturation",
         "2026-01-05", "commerce-team", "supports", 78, 0.85),
        ("INC-2", "checkout", "payments", "db pool exhaustion", "saturation",
         "2026-01-12", "commerce-team", "supports", 78, 0.85),
        ("INC-3", "checkout", "cart", "db pool exhaustion", "saturation",
         "2026-01-12", "commerce-team", "supports", 78, 0.85),
        ("INC-4", "checkout", "edge", "dns resolution failure", "network",
         "2026-01-12", "commerce-team", "insufficient", 40, 0.4),
        ("INC-5", "billing", "invoicer", "cache eviction", "saturation",
         "2026-01-05", "fin-team", "supports", 78, 0.85),
    ]
    results, incidents = [], {}
    for (iid, app, svc, rc, itype, p, owner, status, ev, cp) in rows:
        results.append(_result(iid, svc, rc, itype, status=status,
                               ev_conf=ev, complete=cp))
        incidents[iid] = _inc(iid, app, svc, itype, p, owner)
    return results, incidents


class TestApplicationHealth:
    def test_application_aggregation(self):
        results, inc = _sample()
        out = application_health(results, inc)
        assert out["applications_evaluated"] == 2
        assert set(out["applications"]) == {"checkout", "billing"}
        assert out["investigations"] == 5

    def test_multiple_services_per_application(self):
        results, inc = _sample()
        co = application_health(results, inc)["applications"]["checkout"]
        assert co["services"] == ["cart", "edge", "payments"]
        assert co["services_evaluated"] == 3
        assert co["incidents"] == 4

    def test_worst_of_band_composition(self):
        # checkout has an at_risk service (edge) -> the application is at_risk.
        results, inc = _sample()
        out = application_health(results, inc)
        assert out["applications"]["checkout"]["health_band"] == "at_risk"
        assert out["applications"]["checkout"]["is_healthy"] is False
        assert out["applications"]["billing"]["health_band"] == "healthy"
        assert out["applications"]["billing"]["is_healthy"] is True

    def test_health_score_is_operational_health_rollup(self):
        # the application score IS operational_health's estate rollup — no new
        # scoring is introduced here.
        from sentinel_core.oip import operational_health
        results, inc = _sample()
        co_results = [r for r in results
                      if inc[r["incident_id"]]["application"] == "checkout"]
        co_inc = {i: m for i, m in inc.items() if m["application"] == "checkout"}
        expected = operational_health(co_results, co_inc)["estate_health_score"]
        got = application_health(results, inc)["applications"]["checkout"]
        assert got["health_score"] == expected
        assert got["operational_health"]["estate_health_score"] == expected

    def test_attention_order_worst_first(self):
        results, inc = _sample()
        out = application_health(results, inc)
        assert out["attention_order"][0] == "checkout"   # lower score first
        assert out["band_counts"] == {"healthy": 1, "watch": 0, "at_risk": 1}

    def test_recurring_root_causes_across_services(self):
        # 'db pool exhaustion' recurs across payments+cart within checkout.
        results, inc = _sample()
        co = application_health(results, inc)["applications"]["checkout"]
        rec = co["recurring_root_causes"]
        assert rec[0]["root_cause"] == "db pool exhaustion"
        assert rec[0]["count"] == 3
        assert rec[0]["evidence"] == ["INC-1", "INC-2", "INC-3"]

    def test_incident_trend_propagation(self):
        results, inc = _sample()
        co = application_health(results, inc)["applications"]["checkout"]
        rising = {t["incident_class"] for t in co["what_is_increasing"]}
        assert "saturation" in rising          # 1 -> 2 across periods
        assert co["incident_trends"]["periods"] == ["2026-01-05", "2026-01-12"]

    def test_ownership_propagation(self):
        results, inc = _sample()
        out = application_health(results, inc)
        assert out["applications"]["checkout"]["owner"] == "commerce-team"
        assert out["applications"]["billing"]["owner"] == "fin-team"

    def test_ownership_from_team_field_fallback(self):
        r = [_result("T1", "svc", "cause")]
        inc = {"T1": {"incident_id": "T1", "application": "app-x",
                      "service": "svc", "incident_type": "saturation",
                      "team": "sre-team"}}
        out = application_health(r, inc)["applications"]["app-x"]
        assert out["owner"] == "sre-team"

    def test_ownership_unowned_when_missing(self):
        r = [_result("U1", "svc", "cause")]
        inc = {"U1": {"incident_id": "U1", "application": "app-y",
                      "service": "svc", "incident_type": "saturation"}}
        assert application_health(r, inc)["applications"]["app-y"]["owner"] \
            == "(unowned)"

    def test_driving_incidents_are_non_healthy_services(self):
        results, inc = _sample()
        co = application_health(results, inc)["applications"]["checkout"]
        assert co["driving_incidents"] == ["INC-4"]     # the at_risk edge svc

    def test_confidence_preserved(self):
        results, inc = _sample()
        bi = application_health(results, inc)["applications"]["billing"]
        assert bi["confidence"] == 78                   # from evidence_confidence

    def test_evidence_preserved(self):
        results, inc = _sample()
        co = application_health(results, inc)["applications"]["checkout"]
        assert co["evidence"]["used"] == 15             # 3 * 5 (edge also used 5)
        assert 0.0 <= co["evidence"]["completeness"] <= 1.0

    def test_corpus_version_propagation_verifiable(self):
        results, inc = _sample()
        out = application_health(results, inc)
        assert out["applications"]["checkout"]["verifiable"] is True
        assert out["applications"]["billing"]["verifiable"] is True

    def test_not_verifiable_without_corpus_stamp(self):
        r = [_result("X1", "svc", "cause", corpus="")]
        inc = {"X1": _inc("X1", "app-z", "svc", "saturation", "2026-01-05")}
        assert application_health(r, inc)["applications"]["app-z"]["verifiable"] \
            is False

    def test_next_action_prioritises_investigation(self):
        results, inc = _sample()
        co = application_health(results, inc)["applications"]["checkout"]
        assert co["next_action"].startswith("investigate")

    def test_lone_service_is_its_own_application(self):
        # no application dimension -> the service name is used.
        r = [_result("L1", "lone-svc", "cause")]
        inc = {"L1": {"incident_id": "L1", "service": "lone-svc",
                      "incident_type": "saturation",
                      "created_at": "2026-01-05T00:00:00"}}
        out = application_health(r, inc)
        assert "lone-svc" in out["applications"]

    def test_missing_metadata_unmapped(self):
        # result with no incident metadata at all -> (unmapped) bucket.
        out = application_health([_result("M1", "", "cause")], {})
        assert "(unmapped)" in out["applications"]
        assert out["applications_evaluated"] == 1

    def test_empty_dataset(self):
        out = application_health([])
        assert out["applications_evaluated"] == 0
        assert out["investigations"] == 0
        assert out["estate_health_score"] is None
        assert out["applications"] == {}
        assert out["band_counts"] == {"healthy": 0, "watch": 0, "at_risk": 0}

    def test_deterministic_and_json_safe(self):
        results, inc = _sample()
        a = application_health(results, inc)
        b = application_health(results, inc)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))

    def test_deterministic_ordering(self):
        results, inc = _sample()
        out = application_health(results, inc)
        assert list(out["applications"]) == sorted(out["applications"])
        for app in out["applications"].values():
            assert app["services"] == sorted(app["services"])

    def test_no_new_intelligence_pure_composition(self):
        import importlib
        import inspect
        mod = importlib.import_module("sentinel_core.oip.application_health")
        src = inspect.getsource(mod)
        # composes the two shipped OIP services, nothing else
        assert "operational_health" in src and "incident_trends" in src
        # no reasoning engine, no runtime entrypoint, no bespoke scoring/threshold
        for banned in ("hypothesis_engine", "run_analyze", "compute_confidence",
                       "investigate(", "def _slope", "def _score", "def _trend"):
            assert banned not in src
