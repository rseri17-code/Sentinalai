"""Tranche 3 — enterprise causal reasoning & topology-aware localization.

Coverage: causal graph construction, propagation roles, topology
constraint rejection, temporal causality, blast-radius reasoning,
competing causal chains, elimination reasons, localization, narrative,
shadow contract, determinism.
"""
from __future__ import annotations

import copy
import json

from supervisor.causal_investigation import (
    anchor_hypotheses,
    build_causal_chains,
    build_causal_graph,
    build_narrative,
    classify_roles,
    localize,
    run_causal_investigation,
)


# checkout -> api -> db (checkout depends on api depends on db)
def _evidence(**over):
    ev = {
        "cmdb_blast_radius": {
            "affected_ci": "checkout",
            "blast_radius": {"checkout": [], "api": []},
            "dependency_graph": {"checkout": ["api"], "api": ["db"],
                                  "db": []},
        },
        "trace_correlation": {
            "trace_id": "t1", "root_span_service": "db",
            "error_span": {"service": "db", "error": "pool exhausted"},
            "call_chain": [{"service": "checkout"}, {"service": "api"},
                            {"service": "db", "error": "pool exhausted"}],
            "cross_service_impact": ["checkout", "api", "db"],
        },
    }
    ev.update(over)
    return ev


def _meta():
    return [
        {"name": "db pool exhaustion",
         "root_cause": "db connection pool exhausted", "score": 65,
         "evidence_refs": ["trace_correlation"]},
        {"name": "api latency spike",
         "root_cause": "api service slow response", "score": 55,
         "evidence_refs": []},
    ]


def _run(evidence=None, meta=None, symptom="checkout",
         symptom_time="2026-07-10T08:00:00Z", monkeypatch=None):
    monkeypatch.setenv("CAUSAL_INVESTIGATION_ENABLED", "true")
    r = {"root_cause": "x", "confidence": 70}
    run_causal_investigation(r, evidence or _evidence(), symptom,
                              hypotheses_meta=meta or _meta(),
                              symptom_time=symptom_time)
    return r


# ---------------------------------------------------------------------------
# Shadow contract
# ---------------------------------------------------------------------------

class TestShadowContract:
    def test_flag_off_no_op(self, monkeypatch):
        monkeypatch.delenv("CAUSAL_INVESTIGATION_ENABLED", raising=False)
        r = {"root_cause": "x", "confidence": 70}
        before = copy.deepcopy(r)
        run_causal_investigation(r, _evidence(), "checkout",
                                  hypotheses_meta=_meta())
        assert r == before

    def test_flag_on_additive_only(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["root_cause"] == "x"
        assert r["confidence"] == 70
        ci = r["_causal_investigation"]
        for key in ("causal_graph", "roles", "anchored_hypotheses",
                     "causal_chains", "winning_chain", "eliminated_chains",
                     "localization", "blast_radius", "narrative"):
            assert key in ci
        assert ci == json.loads(json.dumps(ci))       # JSON-safe

    def test_never_raises(self, monkeypatch):
        monkeypatch.setenv("CAUSAL_INVESTIGATION_ENABLED", "true")
        run_causal_investigation({}, {}, "", hypotheses_meta=None)
        run_causal_investigation({"x": 1}, {"cmdb_blast_radius": "bad"},
                                  "svc", hypotheses_meta=[{"z": object()}])

    def test_deterministic(self, monkeypatch):
        outs = []
        for _ in range(2):
            outs.append(json.dumps(
                _run(monkeypatch=monkeypatch)["_causal_investigation"],
                sort_keys=True))
        assert outs[0] == outs[1]

    def test_no_topology_degrades_gracefully(self, monkeypatch):
        r = _run(evidence={}, monkeypatch=monkeypatch)
        ci = r["_causal_investigation"]
        # symptom-only graph, localization defaults to symptom service
        assert ci["localization"]["symptom_service"] == "checkout"


# ---------------------------------------------------------------------------
# Phase 1 — graph
# ---------------------------------------------------------------------------

class TestCausalGraph:
    def test_nodes_and_edges_built(self):
        g = build_causal_graph("checkout", _evidence()).to_dict()
        labels = {n["label"] for n in g["nodes"]}
        assert {"checkout", "api", "db"} <= labels
        etypes = {e["edge_type"] for e in g["edges"]}
        assert "depends_on" in etypes
        assert "observed_in" in etypes

    def test_symptom_node_present(self):
        g = build_causal_graph("checkout", _evidence()).to_dict()
        assert any(n["node_type"] == "symptom" for n in g["nodes"])

    def test_deterministic_ids(self):
        g1 = build_causal_graph("checkout", _evidence()).to_dict()
        g2 = build_causal_graph("checkout", _evidence()).to_dict()
        assert g1 == g2


# ---------------------------------------------------------------------------
# Phase 2 — propagation roles
# ---------------------------------------------------------------------------

class TestRoles:
    def test_downstream_deps_are_root_candidates(self):
        roles = classify_roles("checkout", _evidence())
        assert roles["db"] == "root_candidate"
        assert roles["api"] == "root_candidate"
        assert roles["checkout"] == "symptom"

    def test_upstream_caller_is_victim(self):
        # frontend depends on checkout -> frontend is a victim
        ev = _evidence(cmdb_blast_radius={
            "affected_ci": "checkout",
            "blast_radius": {"checkout": []},
            "dependency_graph": {"frontend": ["checkout"],
                                  "checkout": ["db"], "db": []},
        })
        roles = classify_roles("checkout", ev)
        assert roles["frontend"] == "victim"
        assert roles["db"] == "root_candidate"


# ---------------------------------------------------------------------------
# Phase 3 — topology constraint
# ---------------------------------------------------------------------------

class TestTopologyConstraint:
    def test_downstream_victim_hypothesis_rejected(self, monkeypatch):
        # frontend is a victim (depends on checkout); a hypothesis blaming
        # frontend for the checkout failure is topology-impossible
        ev = _evidence(cmdb_blast_radius={
            "affected_ci": "checkout",
            "blast_radius": {"checkout": []},
            "dependency_graph": {"frontend": ["checkout"],
                                  "checkout": ["db"], "db": []},
        })
        meta = [{"name": "frontend rendering bug",
                  "root_cause": "frontend crash", "score": 80,
                  "evidence_refs": []}]
        roles = classify_roles("checkout", ev)
        anchored = anchor_hypotheses(meta, "checkout", ev, roles)
        fe = [a for a in anchored if a["anchor_service"] == "frontend"][0]
        assert fe["topology_possible"] is False
        assert "topology impossible" in fe["rejection_reason"]

    def test_root_candidate_hypothesis_allowed(self):
        roles = classify_roles("checkout", _evidence())
        anchored = anchor_hypotheses(_meta(), "checkout", _evidence(), roles)
        db = [a for a in anchored if a["anchor_service"] == "db"][0]
        assert db["topology_possible"] is True


# ---------------------------------------------------------------------------
# Phase 6/7 — chains + elimination
# ---------------------------------------------------------------------------

class TestCausalChains:
    def test_error_span_origin_wins(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        ci = r["_causal_investigation"]
        assert ci["winning_chain"]["origin"] == "db"
        assert any("error span" in s
                    for s in ci["winning_chain"]["support"])

    def test_topology_violation_eliminates_chain(self, monkeypatch):
        ev = _evidence(cmdb_blast_radius={
            "affected_ci": "checkout",
            "blast_radius": {"checkout": []},
            "dependency_graph": {"frontend": ["checkout"],
                                  "checkout": ["db"], "db": []},
        })
        meta = [
            {"name": "db pool exhaustion", "root_cause": "db pool", "score": 60,
             "evidence_refs": []},
            {"name": "frontend crash", "root_cause": "frontend", "score": 90,
             "evidence_refs": []},
        ]
        r = _run(evidence=ev, meta=meta, monkeypatch=monkeypatch)
        ci = r["_causal_investigation"]
        elim = {c["origin"] for c in ci["eliminated_chains"]}
        assert "frontend" in elim
        # despite the higher LLM score, the topology-valid db wins
        assert ci["winning_chain"]["origin"] == "db"

    def test_temporal_ordering_eliminates_change(self, monkeypatch):
        ev = _evidence(check_changes={
            "deploys": [{"time": "2026-07-10T09:30:00Z"}]})
        meta = [{"name": "bad db deployment",
                  "root_cause": "db deploy regression", "score": 70,
                  "evidence_refs": []}]
        r = _run(evidence=ev, meta=meta,
                  symptom_time="2026-07-10T08:00:00Z", monkeypatch=monkeypatch)
        ci = r["_causal_investigation"]
        chain = ci["causal_chains"][0]
        assert chain["eliminated"]
        assert any("temporal ordering impossible" in x
                    for x in chain["refutation"])


# ---------------------------------------------------------------------------
# Phase 5 — blast radius
# ---------------------------------------------------------------------------

class TestBlastRadius:
    def test_mismatch_recorded(self, monkeypatch):
        # expected blast = dependents of checkout (frontend); observed
        # = something unrelated → mismatch
        ev = _evidence(cmdb_blast_radius={
            "affected_ci": "checkout",
            "blast_radius": {"unrelated-svc": [{"c": 1}]},
            "dependency_graph": {"frontend": ["checkout"],
                                  "checkout": ["db"], "db": []},
        })
        r = _run(evidence=ev, monkeypatch=monkeypatch)
        ci = r["_causal_investigation"]
        assert "frontend" in ci["blast_radius"]["expected"]
        assert "unrelated-svc" in ci["blast_radius"]["observed"]


# ---------------------------------------------------------------------------
# Phase 8/9 — localization + narrative
# ---------------------------------------------------------------------------

class TestLocalization:
    def test_root_immediate_symptom_split(self, monkeypatch):
        loc = _run(monkeypatch=monkeypatch)["_causal_investigation"][
            "localization"]
        assert loc["root_cause_service"] == "db"
        assert loc["immediate_cause_service"] == "api"
        assert loc["symptom_service"] == "checkout"

    def test_narrative_answers_questions(self, monkeypatch):
        nar = _run(monkeypatch=monkeypatch)["_causal_investigation"][
            "narrative"]
        for key in ("what", "where", "how", "why_survived",
                     "why_others_failed"):
            assert key in nar and isinstance(nar[key], str)
        assert "db" in nar["where"]
        assert "->" in nar["how"]

    def test_narrative_explains_elimination(self, monkeypatch):
        ev = _evidence(cmdb_blast_radius={
            "affected_ci": "checkout",
            "blast_radius": {"checkout": []},
            "dependency_graph": {"frontend": ["checkout"],
                                  "checkout": ["db"], "db": []},
        })
        meta = [
            {"name": "db pool", "root_cause": "db pool", "score": 60,
             "evidence_refs": []},
            {"name": "frontend crash", "root_cause": "frontend", "score": 90,
             "evidence_refs": []},
        ]
        nar = _run(evidence=ev, meta=meta, monkeypatch=monkeypatch)[
            "_causal_investigation"]["narrative"]
        assert "frontend" in nar["why_others_failed"]
