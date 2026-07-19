"""OIP Service #4 — Service Reliability. Produce-only composition tests.

Verifies the per-service reliability rollup answers the SRE's questions from
existing investigation evidence and the shipped OIP services
(operational_health, incident_trends), derives improving/degrading direction
via the reused trend math, preserves evidence/confidence/verifiability
attribution, and stays deterministic — with zero new intelligence and no
runtime touch.
"""
from __future__ import annotations

import json

from sentinel_core.oip import service_reliability


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


def _inc(iid, svc, itype, period, owner=""):
    d = {"incident_id": iid, "service": svc, "incident_type": itype,
         "created_at": period + "T00:00:00", "period": period}
    if owner:
        d["owner"] = owner
    return d


def _sample():
    """payments degrades (healthy P1 -> at_risk P2, db cause recurs 3x);
    edge is a single healthy incident."""
    rows = [
        ("INC-1", "payments", "db pool exhaustion", "saturation", "2026-01-05",
         "pay-team", "supports", 85, 0.9),
        ("INC-2", "payments", "db pool exhaustion", "saturation", "2026-01-12",
         "pay-team", "insufficient", 40, 0.4),
        ("INC-3", "payments", "db pool exhaustion", "saturation", "2026-01-12",
         "pay-team", "insufficient", 42, 0.4),
        ("INC-4", "edge", "cache eviction", "saturation", "2026-01-05",
         "edge-team", "supports", 80, 0.88),
    ]
    results, incidents = [], {}
    for (iid, svc, rc, itype, p, owner, status, ev, cp) in rows:
        results.append(_result(iid, svc, rc, itype, status=status,
                               ev_conf=ev, complete=cp))
        incidents[iid] = _inc(iid, svc, itype, p, owner)
    return results, incidents


class TestServiceReliability:
    def test_service_aggregation(self):
        results, inc = _sample()
        out = service_reliability(results, inc)
        assert out["services_evaluated"] == 2
        assert set(out["services"]) == {"payments", "edge"}
        assert out["investigations"] == 4

    def test_reliability_rollup_score_is_operational_health(self):
        # the reliability score IS operational_health's per-service score.
        from sentinel_core.oip import operational_health
        results, inc = _sample()
        pay_results = [r for r in results
                       if inc[r["incident_id"]]["service"] == "payments"]
        pay_inc = {i: m for i, m in inc.items() if m["service"] == "payments"}
        expected = operational_health(pay_results, pay_inc)[
            "services"]["payments"]["health_score"]
        got = service_reliability(results, inc)["services"]["payments"]
        assert got["reliability_score"] == expected
        assert got["operational_health"]["health_score"] == expected

    def test_reliability_direction_degrading(self):
        results, inc = _sample()
        pay = service_reliability(results, inc)["services"]["payments"]
        assert pay["reliability_direction"] == "degrading"
        assert pay["reliable"] is False
        assert len(pay["reliability_trend"]["series"]) == 2
        assert pay["reliability_trend"]["series"][0] > \
            pay["reliability_trend"]["series"][1]

    def test_reliability_direction_insufficient_history(self):
        results, inc = _sample()
        edge = service_reliability(results, inc)["services"]["edge"]
        assert edge["reliability_direction"] == "insufficient_history"
        assert edge["reliable"] is True

    def test_reliability_direction_improving(self):
        # at_risk P1 -> healthy P2 for one service.
        results = [
            _result("A1", "svc", "db pool exhaustion", status="insufficient",
                    ev_conf=40, complete=0.4),
            _result("A2", "svc", "db pool exhaustion", ev_conf=85,
                    complete=0.92),
        ]
        inc = {"A1": _inc("A1", "svc", "saturation", "2026-01-05"),
               "A2": _inc("A2", "svc", "saturation", "2026-01-12")}
        svc = service_reliability(results, inc)["services"]["svc"]
        assert svc["reliability_direction"] == "improving"
        assert svc["reliability_trend"]["series"][0] < \
            svc["reliability_trend"]["series"][1]

    def test_recurring_failures(self):
        results, inc = _sample()
        pay = service_reliability(results, inc)["services"]["payments"]
        rec = pay["recurring_failures"]
        assert rec[0]["root_cause"] == "db pool exhaustion"
        assert rec[0]["count"] == 3
        assert rec[0]["evidence"] == ["INC-1", "INC-2", "INC-3"]

    def test_ownership_propagation(self):
        results, inc = _sample()
        out = service_reliability(results, inc)
        assert out["services"]["payments"]["owner"] == "pay-team"
        assert out["services"]["edge"]["owner"] == "edge-team"

    def test_ownership_falls_back_to_service_name(self):
        r = [_result("N1", "lonely", "cause")]
        inc = {"N1": _inc("N1", "lonely", "saturation", "2026-01-05")}
        assert service_reliability(r, inc)["services"]["lonely"]["owner"] \
            == "lonely"

    def test_incident_attribution(self):
        results, inc = _sample()
        pay = service_reliability(results, inc)["services"]["payments"]
        assert pay["affecting_incidents"] == ["INC-1", "INC-2", "INC-3"]

    def test_confidence_propagation(self):
        results, inc = _sample()
        edge = service_reliability(results, inc)["services"]["edge"]
        assert edge["confidence"] == 80

    def test_evidence_propagation(self):
        results, inc = _sample()
        pay = service_reliability(results, inc)["services"]["payments"]
        assert "used" in pay["evidence"] and "completeness" in pay["evidence"]
        assert pay["evidence"]["used"] >= 0

    def test_corpus_version_propagation_verifiable(self):
        results, inc = _sample()
        assert service_reliability(results, inc)["services"]["payments"][
            "verifiable"] is True

    def test_not_verifiable_without_corpus_stamp(self):
        r = [_result("X1", "svc", "cause", corpus="")]
        inc = {"X1": _inc("X1", "svc", "saturation", "2026-01-05")}
        assert service_reliability(r, inc)["services"]["svc"]["verifiable"] \
            is False

    def test_fix_first_prioritises_recurring(self):
        results, inc = _sample()
        pay = service_reliability(results, inc)["services"]["payments"]
        assert pay["fix_first"].startswith("fix")

    def test_attention_order_worst_first(self):
        results, inc = _sample()
        out = service_reliability(results, inc)
        # payments (degraded) ranks before edge (healthy)
        assert out["attention_order"][0] == "payments"

    def test_estate_rollup(self):
        results, inc = _sample()
        out = service_reliability(results, inc)
        assert sum(out["band_counts"].values()) == 2
        assert 0.0 <= out["estate_reliability_score"] <= 1.0

    def test_single_service_edge_case(self):
        r = [_result("S1", "solo", "cause")]
        inc = {"S1": _inc("S1", "solo", "saturation", "2026-01-05")}
        out = service_reliability(r, inc)
        assert out["services_evaluated"] == 1
        assert out["services"]["solo"]["reliability_direction"] \
            == "insufficient_history"

    def test_missing_metadata_grouped_as_none(self):
        out = service_reliability([_result("M1", "", "cause")], {})
        assert "(none)" in out["services"]
        assert out["services_evaluated"] == 1

    def test_empty_dataset(self):
        out = service_reliability([])
        assert out["services_evaluated"] == 0
        assert out["investigations"] == 0
        assert out["estate_reliability_score"] is None
        assert out["services"] == {}
        assert out["band_counts"] == {"healthy": 0, "watch": 0, "at_risk": 0}

    def test_deterministic_and_json_safe(self):
        results, inc = _sample()
        a = service_reliability(results, inc)
        b = service_reliability(results, inc)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))

    def test_deterministic_ordering(self):
        results, inc = _sample()
        out = service_reliability(results, inc)
        assert list(out["services"]) == sorted(out["services"])
        for s in out["services"].values():
            assert s["affecting_incidents"] == sorted(s["affecting_incidents"])

    def test_no_new_intelligence_pure_composition(self):
        import importlib
        import inspect
        mod = importlib.import_module("sentinel_core.oip.service_reliability")
        src = inspect.getsource(mod)
        # composes existing OIP services + reused trend math, nothing else
        assert "operational_health" in src and "incident_trends" in src
        assert "_trend" in src
        # no reasoning engine, no runtime entrypoint, no bespoke scoring
        for banned in ("hypothesis_engine", "run_analyze", "compute_confidence",
                       "investigate(", "def _slope", "def _score"):
            assert banned not in src
