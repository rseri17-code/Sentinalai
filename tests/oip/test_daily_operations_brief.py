"""OIP Service #5 — Daily Operations Brief. Produce-only orchestration tests.

Verifies the shift-handoff brief composes the four shipped OIP services into
action-first sections, preserves evidence/confidence/provenance/verifiability
attribution, prioritises worst-first, and stays deterministic — with zero new
intelligence and no runtime touch.
"""
from __future__ import annotations

import json

from sentinel_core.oip import daily_operations_brief


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
    """checkout (payments degrading, cart healthy, edge at_risk; db recurs 3x)
    and billing (healthy)."""
    rows = [
        ("I1", "checkout", "payments", "db pool exhaustion", "saturation",
         "2026-01-05", "pay-team", "supports", 85, 0.9),
        ("I2", "checkout", "payments", "db pool exhaustion", "saturation",
         "2026-01-12", "pay-team", "insufficient", 40, 0.4),
        ("I3", "checkout", "cart", "db pool exhaustion", "saturation",
         "2026-01-12", "pay-team", "supports", 78, 0.85),
        ("I4", "checkout", "edge", "dns resolution failure", "network",
         "2026-01-12", "pay-team", "insufficient", 41, 0.4),
        ("I5", "billing", "invoicer", "cache eviction", "saturation",
         "2026-01-05", "fin-team", "supports", 80, 0.88),
    ]
    results, incidents = [], {}
    for (iid, app, svc, rc, itype, p, owner, status, ev, cp) in rows:
        results.append(_result(iid, svc, rc, itype, status=status,
                               ev_conf=ev, complete=cp))
        incidents[iid] = _inc(iid, app, svc, itype, p, owner)
    return results, incidents


class TestDailyOperationsBrief:
    def test_brief_structure_and_period(self):
        results, inc = _sample()
        b = daily_operations_brief(results, inc, period="overnight")
        assert b["period"] == "overnight"
        assert b["periods_covered"] == ["2026-01-05", "2026-01-12"]
        assert b["investigations"] == 5
        for section in ("critical_services", "applications_at_risk",
                        "significant_incident_trends", "recurring_failures",
                        "changed_since_previous", "highest_priority_actions",
                        "verification_status", "headline"):
            assert section in b

    def test_headline_counts(self):
        results, inc = _sample()
        h = daily_operations_brief(results, inc)["headline"]
        assert h["services_evaluated"] == 4
        assert h["applications_evaluated"] == 2
        assert h["critical_services"] == len(
            daily_operations_brief(results, inc)["critical_services"])

    def test_critical_services_worst_first_with_evidence(self):
        results, inc = _sample()
        crit = daily_operations_brief(results, inc)["critical_services"]
        # only non-healthy services appear
        assert all(c["reliability_band"] != "healthy" for c in crit)
        names = [c["service"] for c in crit]
        assert "edge" in names and "payments" in names
        assert "cart" not in names           # cart is healthy
        # worst first: at_risk edge before watch payments
        assert names.index("edge") < names.index("payments")
        # every entry references supporting incidents
        for c in crit:
            assert c["evidence"] and all(e.startswith("I") for e in c["evidence"])

    def test_applications_at_risk_with_evidence(self):
        results, inc = _sample()
        risk = daily_operations_brief(results, inc)["applications_at_risk"]
        assert [a["application"] for a in risk] == ["checkout"]
        assert risk[0]["health_band"] == "at_risk"
        assert risk[0]["evidence"]           # driving incidents present
        assert risk[0]["owner"] == "pay-team"

    def test_significant_incident_trends(self):
        results, inc = _sample()
        trends = daily_operations_brief(results, inc)[
            "significant_incident_trends"]
        assert all(t["verdict"] == "increasing" for t in trends)
        assert any(t["incident_class"] == "network" for t in trends)

    def test_recurring_failures_with_evidence(self):
        results, inc = _sample()
        rec = daily_operations_brief(results, inc)["recurring_failures"]
        assert rec[0]["root_cause"] == "db pool exhaustion"
        assert rec[0]["count"] == 3
        assert rec[0]["evidence"] == ["I1", "I2", "I3"]

    def test_highest_priority_actions_ranked_with_evidence(self):
        results, inc = _sample()
        actions = daily_operations_brief(results, inc)[
            "highest_priority_actions"]
        assert actions
        assert actions[0]["priority"] == "recurring_failure"
        for a in actions:
            assert "evidence" in a

    def test_changed_since_previous(self):
        results, inc = _sample()
        delta = daily_operations_brief(results, inc)["changed_since_previous"]
        assert delta["available"] is True
        assert delta["previous"] == "2026-01-05"
        assert delta["current"] == "2026-01-12"

    def test_verification_status(self):
        results, inc = _sample()
        v = daily_operations_brief(results, inc)["verification_status"]
        assert v["verifiable"] is True
        assert v["investigations"] == 5
        assert v["corpus_stamped"] == 5

    def test_verification_status_partial_stamp(self):
        results, inc = _sample()
        results.append(_result("I6", "payments", "db pool exhaustion",
                               corpus=""))
        inc["I6"] = _inc("I6", "checkout", "payments", "saturation",
                         "2026-01-12", "pay-team")
        v = daily_operations_brief(results, inc)["verification_status"]
        assert v["verifiable"] is False
        assert v["corpus_stamped"] == 5 and v["investigations"] == 6

    def test_multiple_applications_and_services(self):
        results, inc = _sample()
        b = daily_operations_brief(results, inc)
        assert b["headline"]["applications_evaluated"] == 2
        assert b["headline"]["services_evaluated"] == 4

    def test_empty_period(self):
        b = daily_operations_brief([])
        assert b["investigations"] == 0
        assert b["critical_services"] == []
        assert b["applications_at_risk"] == []
        assert b["significant_incident_trends"] == []
        assert b["recurring_failures"] == []
        assert b["highest_priority_actions"] == []
        assert b["verification_status"] == {
            "verifiable": True, "investigations": 0, "corpus_stamped": 0}
        assert b["headline"]["critical_services"] == 0

    def test_confidence_and_provenance_preserved(self):
        # every recommendation traces to incident ids, and verifiability
        # flows from the R1 corpus stamp.
        results, inc = _sample()
        b = daily_operations_brief(results, inc)
        assert all(c["verifiable"] for c in b["critical_services"])
        assert all(a["verifiable"] for a in b["applications_at_risk"])

    def test_deterministic_and_json_safe(self):
        results, inc = _sample()
        a = daily_operations_brief(results, inc, period="W03")
        b = daily_operations_brief(results, inc, period="W03")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))

    def test_stable_ordering(self):
        results, inc = _sample()
        b = daily_operations_brief(results, inc)
        crit_scores = [c["reliability_score"] for c in b["critical_services"]]
        assert crit_scores == sorted(crit_scores)   # worst (lowest) first

    def test_no_new_intelligence_pure_composition(self):
        import importlib
        import inspect
        mod = importlib.import_module(
            "sentinel_core.oip.daily_operations_brief")
        src = inspect.getsource(mod)
        # orchestrates the four shipped OIP services and nothing else
        for svc in ("operational_health", "incident_trends",
                    "application_health", "service_reliability"):
            assert svc in src
        # no reasoning engine, no runtime entrypoint, no bespoke scoring/trend
        for banned in ("hypothesis_engine", "run_analyze", "compute_confidence",
                       "investigate(", "def _slope", "def _score",
                       "def _trend", "observation_record", "bucket_by"):
            assert banned not in src
