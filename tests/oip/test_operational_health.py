"""OIP Service #1 — Operational Health. Produce-only composition tests.

Verifies the operator-facing rollup answers the 8 product questions from
existing investigation evidence, triages worst-first, surfaces recurring
causes and degraded sources, and stays deterministic — with zero new
intelligence and no runtime touch.
"""
from __future__ import annotations

import json

from sentinel_core.oip import operational_health


def _result(iid, svc, rc, status="supports", ev_conf=78, complete=0.85,
            degraded=None):
    r = {
        "incident_id": iid, "root_cause": rc, "confidence": 80,
        "incident_type": "saturation", "_corpus_version": "corpus:abc",
        "_investigation_validation": {
            "root_cause_verification": {"verification_status": status},
            "evidence_validation": {"evidence_validation_score": 0.85},
            "confidence_reconstruction": {"evidence_confidence": ev_conf},
            "investigation_completeness": {
                "investigation_completeness_score": complete},
            "expert_concordance": {"independent_winner": rc}},
        "_decision_intelligence": {
            "decision_arbitration": {"winner": rc},
            "decision_stability": {"stable": True},
            "decision_quality": {"overall_decision_quality": 0.85}},
        "_causal_investigation": {"localization": {"root_cause_service": svc}},
        "_evidence_lifecycle": {"counts": {"used": 5, "filtered": 0,
                                            "unavailable": 0, "error": 0}},
    }
    if degraded:
        r["degraded_investigation"] = True
        r["_sources_unavailable"] = [{"source": degraded, "reason": "timeout",
                                       "state": "unavailable"}]
        r["_evidence_lifecycle"]["counts"] = {"used": 3, "unavailable": 1,
                                              "error": 0, "filtered": 0}
    return r


def _incidents(*ids_svcs):
    return {i: {"incident_id": i, "service": s, "incident_type": "saturation"}
            for i, s in ids_svcs}


def _sample():
    inc = _incidents(("INC-1", "payments"), ("INC-2", "payments"),
                     ("INC-3", "checkout"), ("INC-4", "edge"))
    results = [
        _result("INC-1", "payments", "db pool exhaustion"),
        _result("INC-2", "payments", "db pool exhaustion"),
        _result("INC-3", "checkout", "regression in deployment v4.2"),
        _result("INC-4", "edge", "dns resolution failure",
                status="insufficient", ev_conf=40, complete=0.4,
                degraded="knowledge_graph"),
    ]
    return results, inc


class TestOperationalHealth:
    def test_per_service_rollup(self):
        results, inc = _sample()
        h = operational_health(results, inc)
        assert h["services_evaluated"] == 3
        assert set(h["services"]) == {"payments", "checkout", "edge"}

    def test_worst_first_triage(self):
        results, inc = _sample()
        h = operational_health(results, inc)
        assert h["attention_order"][0] == "edge"        # lowest score first

    def test_degraded_service_at_risk_with_action(self):
        results, inc = _sample()
        edge = operational_health(results, inc)["services"]["edge"]
        assert edge["health_band"] == "at_risk"
        assert "knowledge_graph" in edge["next_action"]
        assert edge["degraded_sources"] == ["knowledge_graph"]

    def test_recurring_root_cause_surfaced(self):
        results, inc = _sample()
        pay = operational_health(results, inc)["services"]["payments"]
        assert pay["recurring_root_causes"] == [["db pool exhaustion", 2]]
        assert "recurring" in pay["next_action"]

    def test_eight_operator_questions_present(self):
        results, inc = _sample()
        pay = operational_health(results, inc)["services"]["payments"]
        for q in ("what_happened", "why", "evidence", "what_changed", "owner",
                   "next_action", "confidence", "verifiable"):
            assert q in pay
        assert pay["verifiable"] is True          # R1 corpus_version present
        assert pay["evidence"]["used"] == 5

    def test_what_changed_flag_for_deployment(self):
        results, inc = _sample()
        co = operational_health(results, inc)["services"]["checkout"]
        assert co["what_changed"] is True          # deployment regression

    def test_healthy_service_no_action(self):
        inc = _incidents(("INC-1", "billing"))
        h = operational_health([_result("INC-1", "billing", "cache eviction")],
                               inc)
        assert h["services"]["billing"]["health_band"] == "healthy"
        assert h["services"]["billing"]["next_action"] == "no action — healthy"

    def test_estate_rollup_bands(self):
        results, inc = _sample()
        h = operational_health(results, inc)
        assert sum(h["band_counts"].values()) == 3
        assert 0.0 <= h["estate_health_score"] <= 1.0

    def test_empty(self):
        h = operational_health([])
        assert h["services_evaluated"] == 0
        assert h["estate_health_score"] is None

    def test_deterministic_and_json_safe(self):
        results, inc = _sample()
        a = operational_health(results, inc, period="W03")
        b = operational_health(results, inc, period="W03")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))

    def test_no_new_intelligence_pure_composition(self):
        # the service imports only the reused shadow_pilot helpers — no engine
        import inspect
        from sentinel_core.oip import operational_health as mod
        src = inspect.getsource(mod)
        assert "observation_record" in src and "bucket_by" in src
        # composes existing outputs; does not import any reasoning engine
        for banned in ("hypothesis_engine", "run_analyze", "compute_confidence",
                        "investigate("):
            assert banned not in src
