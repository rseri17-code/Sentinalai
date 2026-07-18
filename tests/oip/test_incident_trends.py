"""OIP Service #2 — Incident Trends. Produce-only composition tests.

Verifies the estate-wide trend rollup answers the operator's questions —
what is increasing, what recurs, what changed since last period, which
services are getting worse, what to investigate first — from existing
investigation evidence only, staying deterministic with zero new
intelligence and no runtime touch.
"""
from __future__ import annotations

import json

from sentinel_core.oip import incident_trends


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


def _inc(iid, svc, itype, period):
    return {"incident_id": iid, "service": svc, "incident_type": itype,
            "created_at": period + "T00:00:00", "period": period}


def _sample():
    """Two periods; saturation rises (1 -> 2), network appears (0 -> 1),
    deploy falls (1 -> 0); 'db pool exhaustion' recurs 3x."""
    rows = [
        ("INC-1", "payments", "db pool exhaustion", "saturation", "2026-01-05"),
        ("INC-2", "payments", "db pool exhaustion", "saturation", "2026-01-12"),
        ("INC-3", "checkout", "db pool exhaustion", "saturation", "2026-01-12"),
        ("INC-4", "edge", "regression in deployment", "deploy", "2026-01-05"),
        ("INC-5", "edge", "dns resolution failure", "network", "2026-01-12"),
    ]
    results, incidents = [], {}
    for iid, svc, rc, itype, p in rows:
        results.append(_result(iid, svc, rc, itype))
        incidents[iid] = _inc(iid, svc, itype, p)
    return results, incidents


class TestIncidentTrends:
    def test_periods_ordered_from_incident_timestamps(self):
        results, inc = _sample()
        out = incident_trends(results, inc)
        assert out["periods"] == ["2026-01-05", "2026-01-12"]
        assert out["investigations"] == 5

    def test_grouping_by_incident_class(self):
        results, inc = _sample()
        classes = {t["incident_class"] for t in
                   incident_trends(results, inc)["class_trends"]}
        assert classes == {"saturation", "deploy", "network"}

    def test_trend_calculation_increasing_class(self):
        results, inc = _sample()
        by = {t["incident_class"]: t
              for t in incident_trends(results, inc)["class_trends"]}
        assert by["saturation"]["verdict"] == "increasing"   # 1 -> 2
        assert by["saturation"]["series"] == [1, 2]
        assert by["saturation"]["slope"] > 0

    def test_trend_calculation_decreasing_class(self):
        results, inc = _sample()
        by = {t["incident_class"]: t
              for t in incident_trends(results, inc)["class_trends"]}
        assert by["deploy"]["verdict"] == "decreasing"       # 1 -> 0
        assert by["deploy"]["series"] == [1, 0]

    def test_what_is_increasing_filtered_view(self):
        results, inc = _sample()
        rising = {t["incident_class"]
                  for t in incident_trends(results, inc)["what_is_increasing"]}
        assert "saturation" in rising and "network" in rising
        assert "deploy" not in rising

    def test_recurring_failure_aggregation(self):
        results, inc = _sample()
        rec = incident_trends(results, inc)["what_is_recurring"]
        assert rec[0]["root_cause"] == "db pool exhaustion"
        assert rec[0]["count"] == 3
        assert rec[0]["evidence"] == ["INC-1", "INC-2", "INC-3"]

    def test_non_recurring_cause_excluded(self):
        results, inc = _sample()
        causes = {r["root_cause"]
                  for r in incident_trends(results, inc)["what_is_recurring"]}
        assert "dns resolution failure" not in causes   # appears once only

    def test_changed_since_previous_period_delta(self):
        results, inc = _sample()
        delta = incident_trends(results, inc)["changed_since_previous"]
        assert delta["available"] is True
        assert delta["previous"] == "2026-01-05"
        assert delta["current"] == "2026-01-12"
        assert delta["by_class"]["saturation"]["delta"] == 1     # 1 -> 2
        assert delta["by_class"]["deploy"]["delta"] == -1        # 1 -> 0

    def test_services_getting_worse_health_decline(self):
        # 'edge' resolves a deploy in P1 but fails a network incident in P2.
        results = [
            _result("A1", "edge", "regression in deployment", "deploy"),
            _result("A2", "edge", "dns resolution failure", "network",
                    status="insufficient", ev_conf=40, complete=0.4),
        ]
        inc = {"A1": _inc("A1", "edge", "deploy", "2026-01-05"),
               "A2": _inc("A2", "edge", "network", "2026-01-12")}
        worse = incident_trends(results, inc)["services_getting_worse"]
        assert worse and worse[0]["service"] == "edge"
        assert worse[0]["decline"] > 0.0
        assert worse[0]["current_score"] < worse[0]["previous_score"]

    def test_investigate_first_ranks_signals(self):
        results, inc = _sample()
        first = incident_trends(results, inc)["investigate_first"]
        priorities = {i["priority"] for i in first}
        assert "recurring_failure" in priorities
        assert "increasing_incident_class" in priorities
        # highest-signal first; recurring cause (3x) leads
        assert first[0]["priority"] == "recurring_failure"
        assert first[0]["target"] == "db pool exhaustion"

    def test_confidence_and_provenance_preserved(self):
        # every conclusion traces to investigation ids (evidence) and the R1
        # corpus stamp makes the whole rollup verifiable.
        results, inc = _sample()
        out = incident_trends(results, inc)
        assert out["verifiable"] is True
        for t in out["class_trends"]:
            assert all(e.startswith("INC-") for e in t["evidence"])
        for r in out["what_is_recurring"]:
            assert r["evidence"] == sorted(r["evidence"])

    def test_not_verifiable_without_corpus_stamp(self):
        results = [_result("X1", "svc", "cause", corpus=""),
                   _result("X2", "svc", "cause", corpus="")]
        inc = {"X1": _inc("X1", "svc", "saturation", "2026-01-05"),
               "X2": _inc("X2", "svc", "saturation", "2026-01-12")}
        assert incident_trends(results, inc)["verifiable"] is False

    def test_single_period_has_no_delta_or_decline(self):
        results = [_result("S1", "svc", "cause")]
        inc = {"S1": _inc("S1", "svc", "saturation", "2026-01-05")}
        out = incident_trends(results, inc)
        assert out["changed_since_previous"]["available"] is False
        assert out["services_getting_worse"] == []

    def test_empty_dataset(self):
        out = incident_trends([])
        assert out["investigations"] == 0
        assert out["periods"] == []
        assert out["what_is_increasing"] == []
        assert out["what_is_recurring"] == []
        assert out["investigate_first"] == []
        assert out["verifiable"] is True
        assert out["changed_since_previous"]["available"] is False

    def test_undated_incidents_excluded_from_periods(self):
        results = [_result("U1", "svc", "cause")]
        inc = {"U1": {"incident_id": "U1", "service": "svc",
                      "incident_type": "saturation"}}   # no timestamp
        out = incident_trends(results, inc)
        assert out["periods"] == []
        assert out["investigations"] == 1

    def test_deterministic_and_json_safe(self):
        results, inc = _sample()
        a = incident_trends(results, inc)
        b = incident_trends(results, inc)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))

    def test_no_new_intelligence_pure_composition(self):
        import importlib
        import inspect
        mod = importlib.import_module("sentinel_core.oip.incident_trends")
        src = inspect.getsource(mod)
        # reuses existing helpers, does not reimplement them
        assert "observation_record" in src and "bucket_by" in src
        assert "_trend" in src and "operational_health" in src
        # no reasoning engine, no runtime entrypoint, no bespoke scoring
        for banned in ("hypothesis_engine", "run_analyze", "compute_confidence",
                       "investigate(", "def _slope", "def _score"):
            assert banned not in src
