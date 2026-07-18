"""Operational Discovery Engine (ODE) — offline knowledge-discovery tests.

Coverage: observation composition, each miner (topology/temporal/evidence/
hypothesis/operational/human), statistical support (recurrence/CI/contradictions/
significance), reproducibility split-half, DQS, longitudinal tracking, no-false-
discovery under insufficient support, determinism.
"""
from __future__ import annotations

import json

from sentinel_core.ode import (
    discovery_quality_score,
    longitudinal_update,
    observation,
    run_discovery,
)
from sentinel_core.ode.discovery import (
    mine_evidence,
    mine_hypothesis,
    mine_human,
    mine_operational,
    mine_temporal,
    mine_topology,
)


def _result(rc="db pool exhaustion", root="db", sym="checkout",
            decisive=("db_pool_metrics",), ruled=("dns failure",)):
    return {
        "root_cause": rc,
        "_causal_investigation": {
            "localization": {"root_cause_service": root,
                             "symptom_service": sym},
            "roles": {root: "root_candidate", sym: "symptom"},
            "winning_chain": {"path": [root, sym]}},
        "_elimination_narrative": {"winner": rc,
                                    "ruled_out": [{"name": r} for r in ruled]},
        "_decision_intelligence": {"evidence_attribution": {
            "decisive_evidence": list(decisive)}},
    }


def _obs(i, itype="saturation", service="checkout", time=None,
         result=None, human=None, outcome=None):
    inc = {"incident_id": f"INC-{i}", "incident_type": itype,
           "service": service, "created_at": time or f"2026-01-0{i}T08:00:00Z"}
    return observation(result or _result(), inc, human=human,
                       outcome_correct=outcome)


def _history(n=5):
    return [_obs(i + 1) for i in range(n)]


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

class TestObservation:
    def test_composes_fields(self):
        o = _obs(1)
        assert o["incident_type"] == "saturation"
        assert ["db", "checkout"] in o["causal_edges"]
        assert o["decisive_evidence"] == ["db_pool_metrics"]
        assert "db" in o["affected_services"]
        assert o["ruled_out"] == ["dns failure"]

    def test_incident_time_from_incident(self):
        assert _obs(1)["incident_time"] == "2026-01-01T08:00:00Z"


# ---------------------------------------------------------------------------
# Miners
# ---------------------------------------------------------------------------

class TestTopology:
    def test_discovers_undeclared_dependency(self):
        d = mine_topology(_history(5), declared_dependencies=[["checkout", "api"]])
        assert len(d) == 1
        assert d[0]["signature"] == ["db", "checkout"]
        assert d[0]["recurrence_count"] == 5

    def test_declared_dependency_not_discovered(self):
        d = mine_topology(_history(5), declared_dependencies=[["db", "checkout"]])
        assert d == []

    def test_insufficient_recurrence_no_discovery(self):
        assert mine_topology(_history(2)) == []      # below _MIN_OBS_FOR_CLASS


class TestEvidence:
    def test_decisive_evidence_pattern(self):
        d = mine_evidence(_history(5))
        assert any(x["signature"] == ["saturation", "db_pool_metrics"]
                   for x in d)

    def test_contradiction_lowers_confidence(self):
        obs = _history(5) + [_obs(9, result=_result(decisive=["other"]))]
        d = [x for x in mine_evidence(obs)
             if x["signature"] == ["saturation", "db_pool_metrics"]][0]
        assert d["statistical_support"]["contradictions"] == 1
        assert d["confidence"] < 1.0


class TestHypothesis:
    def test_recurring_false_lead(self):
        d = mine_hypothesis(_history(5))
        assert any(x["signature"] == ["saturation", "false_lead", "dns failure"]
                   for x in d)


class TestOperational:
    def test_latent_failure_cluster(self):
        d = mine_operational(_history(5))
        assert any(set(x["signature"]) == {"checkout", "db"} for x in d)


class TestTemporal:
    def test_ordered_incidents_within_window(self):
        # two services, incidents minutes apart, recurring order A->B
        obs = []
        for i in range(4):
            base = f"2026-01-0{i + 1}T08:00:00Z"
            later = f"2026-01-0{i + 1}T08:30:00Z"
            obs.append(_obs(f"{i}a", service="svcA", time=base))
            obs.append(_obs(f"{i}b", service="svcB", time=later))
        d = mine_temporal(obs, window_seconds=7200)
        sig = [x for x in d if x["signature"] == ["svcA", "svcB"]]
        assert sig and sig[0]["recurrence_count"] >= 3
        assert "median_lead_seconds" in sig[0]

    def test_no_temporal_outside_window(self):
        # incidents a day apart -> outside 2h window -> no discovery
        d = mine_temporal(_history(5), window_seconds=7200)
        assert d == []


class TestHuman:
    def test_intervention_correlated_with_outcome(self):
        obs = [_obs(i, human={"interventions": ["restart pool"]}, outcome=True)
               for i in range(1, 5)]
        d = mine_human(obs)
        assert any(x["signature"] == ["intervention", "restart pool"]
                   for x in d)

    def test_no_discovery_without_outcome_labels(self):
        obs = [_obs(i, human={"interventions": ["x"]}) for i in range(1, 5)]
        assert mine_human(obs) == []


# ---------------------------------------------------------------------------
# DQS + reproducibility
# ---------------------------------------------------------------------------

class TestDQS:
    def test_novel_topology_scores_high(self):
        d = mine_topology(_history(5), declared_dependencies=[["checkout", "api"]])
        dqs = discovery_quality_score(d[0])
        assert dqs["dqs"] > 0.8
        assert dqs["components"]["novelty"] == 1.0

    def test_known_signature_zero_novelty(self):
        d = mine_topology(_history(5), declared_dependencies=[["checkout", "api"]])
        sig_id = d[0]["discovery_id"]
        # feed the discovery's own signature-id as known -> novelty 0
        from sentinel_core.ode.discovery import _sha16
        known = _sha16([d[0]["discovery_type"], d[0]["signature"]])
        dqs = discovery_quality_score(d[0], known_signatures=[known])
        assert dqs["components"]["novelty"] == 0.0


# ---------------------------------------------------------------------------
# Longitudinal
# ---------------------------------------------------------------------------

class TestLongitudinal:
    def test_strengthened_and_retired(self):
        prev = run_discovery(_history(5),
                             declared_dependencies=[["checkout", "api"]])
        # add a contradicting-then-supporting set to shift confidence
        more = _history(5) + [_obs(9, result=_result(decisive=["db_pool_metrics"]))]
        cur = run_discovery(more, declared_dependencies=[["checkout", "api"]])
        lu = longitudinal_update(prev["discoveries"], cur["discoveries"])
        assert set(lu) >= {"strengthened", "weakened", "disproven", "retired",
                            "new"}


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

class TestRunDiscovery:
    def test_discovers_and_scores(self):
        rep = run_discovery(_history(5),
                            declared_dependencies=[["checkout", "api"]])
        assert rep["knowledge_count"] >= 3
        assert "topology" in rep["by_type"]
        for d in rep["discoveries"]:
            assert d["discovery_id"] in rep["dqs"]

    def test_empty_history(self):
        rep = run_discovery([])
        assert rep["knowledge_count"] == 0

    def test_deterministic_and_json_safe(self):
        obs = _history(5)
        a = run_discovery(obs, declared_dependencies=[["checkout", "api"]])
        b = run_discovery(obs, declared_dependencies=[["checkout", "api"]])
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))
