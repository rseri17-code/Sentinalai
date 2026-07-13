"""Tranche 1 — hypothesis-centric investigation engine tests.

Mission Phase 9 coverage: belief revision, hypothesis elimination,
active disconfirmation, confidence evolution, transition history,
counterfactual reporting, wrong-hypothesis rejection,
multi-hypothesis convergence, flag-off no-op, determinism.
"""
from __future__ import annotations

import copy
import json

from supervisor.hypothesis_engine import (
    run_hypothesis_engine,
    select_disconfirmation_probe,
)


def _meta():
    """Ranked differential as _analyze_evidence now exports it."""
    return [
        {"name": "OOM kill on checkout",
         "root_cause": "checkout container OOMKilled — memory limit too low",
         "score": 70.0,
         "evidence_refs": ["oom_events", "pod_lifecycle"]},
        {"name": "bad deployment rollback needed",
         "root_cause": "regression introduced by deploy 4412",
         "score": 55.0,
         "evidence_refs": ["get_recent_deployments", "diff_analysis"]},
        {"name": "dns resolution failure",
         "root_cause": "coredns nxdomain for checkout upstream",
         "score": 40.0,
         "evidence_refs": ["dns_records"]},
    ]


def _evidence(**extra):
    ev = {
        "oom_events": {"count": 41},
        "pod_lifecycle": {"restarts": 42},
        "get_recent_deployments": {"deploys": ["4412"]},
        "diff_analysis": {"risky": True},
        # dns_records deliberately ABSENT — third hypothesis uncited
        "_suggested_root_causes": [
            {"cause": "connection pool exhausted at checkout db"},
        ],
    }
    ev.update(extra)
    return ev


def _result(**over):
    r = {"incident_id": "INC-T1",
          "root_cause": "checkout container OOMKilled — memory limit too low",
          "confidence": 78,
          "_critique": {"gaps": ["certificate"]}}
    r.update(over)
    return r


def _run(result=None, evidence=None, meta=None, monkeypatch=None, **kw):
    monkeypatch.setenv("HYPOTHESIS_ENGINE_ENABLED", "true")
    r = result or _result()
    run_hypothesis_engine(r, evidence or _evidence(), "kubernetes",
                            hypotheses_meta=meta or _meta(), **kw)
    return r


# ---------------------------------------------------------------------------
# Flag-off contract
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_off_result_untouched(self, monkeypatch):
        monkeypatch.delenv("HYPOTHESIS_ENGINE_ENABLED", raising=False)
        r = _result()
        before = copy.deepcopy(r)
        run_hypothesis_engine(r, _evidence(), "kubernetes",
                                hypotheses_meta=_meta())
        assert r == before

    def test_never_raises_on_garbage(self, monkeypatch):
        monkeypatch.setenv("HYPOTHESIS_ENGINE_ENABLED", "true")
        run_hypothesis_engine({}, {}, "", hypotheses_meta=None)
        run_hypothesis_engine({"root_cause": ""}, {}, "x",
                                hypotheses_meta=[{"bad": object()}])


# ---------------------------------------------------------------------------
# Multi-hypothesis convergence + shadow authority
# ---------------------------------------------------------------------------

class TestConvergence:
    def test_graph_attached_with_competing_hypotheses(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        g = r["_hypothesis_graph"]
        # 3 differential + 1 non-duplicate prior suggestion
        assert len(g["hypotheses"]) == 4
        statuses = {h["status"] for h in g["hypotheses"]}
        assert "confirmed" in statuses
        assert "ruled_out" in statuses

    def test_winner_is_best_supported(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        confirmed = [h for h in r["_hypothesis_graph"]["hypotheses"]
                     if h["status"] == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["name"] == "OOM kill on checkout"

    def test_shadow_authority_root_cause_untouched(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["root_cause"] == \
            "checkout container OOMKilled — memory limit too low"
        assert r["confidence"] == 78

    def test_prior_suggestion_becomes_candidate(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        names = [h["name"] for h in r["_hypothesis_graph"]["hypotheses"]]
        assert any("pool exhausted" in n for n in names)
        origin = [c for c in r["_elimination_narrative"]["considered"]
                  if "pool exhausted" in c["name"]]
        assert origin[0]["origin"] == "prior"


# ---------------------------------------------------------------------------
# Belief revision + transition history
# ---------------------------------------------------------------------------

class TestBeliefRevision:
    def test_confidence_evolves_per_evidence_event(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        winner = [h for h in r["_hypothesis_graph"]["hypotheses"]
                  if h["status"] == "confirmed"][0]
        support_ts = [t for t in winner["transitions"]
                      if t["reason"].startswith("support:")]
        assert len(support_ts) == 2                 # oom_events, pod_lifecycle
        # each transition records before/after and moves upward
        for t in support_ts:
            assert t["confidence_after"] > t["confidence_before"]

    def test_transition_ordering_deterministic(self, monkeypatch):
        r1 = _run(monkeypatch=monkeypatch)
        r2 = _run(monkeypatch=monkeypatch)
        assert json.dumps(r1["_hypothesis_graph"], sort_keys=True) \
            == json.dumps(r2["_hypothesis_graph"], sort_keys=True)

    def test_uncited_evidence_not_attached(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        dns = [h for h in r["_hypothesis_graph"]["hypotheses"]
               if h["name"] == "dns resolution failure"][0]
        # dns_records absent from evidence → no support attached
        assert dns["supporting_evidence"] == []


# ---------------------------------------------------------------------------
# Active disconfirmation
# ---------------------------------------------------------------------------

class TestDisconfirmation:
    def test_competing_evidence_refutes_leader(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        winner = [h for h in r["_hypothesis_graph"]["hypotheses"]
                  if h["status"] == "confirmed"][0]
        refuting = [e["key"] for e in winner["refuting_evidence"]]
        # rival-cited evidence attacked the leader (bounded to 3)
        assert "diff_analysis" in refuting or \
            "get_recent_deployments" in refuting
        assert len(refuting) <= 4

    def test_leader_flips_when_rival_evidence_dominates(self, monkeypatch):
        # leader has NO cited evidence in hand; rival has plenty
        meta = [
            {"name": "phantom cause", "root_cause": "phantom",
             "score": 90.0, "evidence_refs": ["missing_key"]},
            {"name": "real cause", "root_cause": "real",
             "score": 88.0,
             "evidence_refs": ["oom_events", "pod_lifecycle",
                                "get_recent_deployments"]},
        ]
        r = _run(meta=meta, monkeypatch=monkeypatch,
                  evidence=_evidence())
        narrative = r["_elimination_narrative"]
        assert narrative["winner"] == "real cause"
        assert narrative["survived_disconfirmation"] is False

    def test_survival_recorded_when_leader_holds(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["_elimination_narrative"]["survived_disconfirmation"] \
            is True

    def test_probe_selected_from_capability_catalog(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        probe = r["_elimination_narrative"]["refutation_probe"]
        assert probe is not None
        assert probe["capability_id"].startswith("cap:")
        assert probe["expected_value"] > 0
        assert probe["missing_evidence"]

    def test_probe_selection_pure_and_deterministic(self):
        keys = {"oom_events", "logs"}
        p1 = select_disconfirmation_probe("kubernetes", keys)
        p2 = select_disconfirmation_probe("kubernetes", keys)
        assert p1 == p2

    def test_probe_not_executed_without_fetch_flag(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["_elimination_narrative"]["probe_outcome"]["executed"] \
            is False

    def test_bounded_probe_execution_with_stub_sup(self, monkeypatch):
        monkeypatch.setenv("HYPOTHESIS_DISCONFIRMATION_FETCH", "true")

        calls = []

        class _Worker:
            def execute(self, action, params):
                calls.append(action)
                return {}                      # empty → no refutation found

        class _Sup:
            workers = {"event_worker": _Worker(),
                        "change_worker": _Worker(),
                        "log_worker": _Worker(),
                        "signal_worker": _Worker(),
                        "metrics_worker": _Worker()}

        class _Budget:
            def remaining(self):
                return 10

        ev = _evidence()
        r = _run(monkeypatch=monkeypatch, evidence=ev,
                  sup=_Sup(), budget=_Budget())
        outcome = r["_elimination_narrative"]["probe_outcome"]
        assert outcome["executed"] is True
        assert len(calls) == 1                  # ONE bounded round
        assert outcome["evidence_key"] in ev    # merged into evidence

    def test_probe_skipped_when_budget_exhausted(self, monkeypatch):
        monkeypatch.setenv("HYPOTHESIS_DISCONFIRMATION_FETCH", "true")

        class _Budget:
            def remaining(self):
                return 0

        r = _run(monkeypatch=monkeypatch, budget=_Budget())
        outcome = r["_elimination_narrative"]["probe_outcome"]
        assert outcome["executed"] is False
        assert outcome.get("skip_reason") == "budget"


# ---------------------------------------------------------------------------
# Elimination narrative + counterfactual
# ---------------------------------------------------------------------------

class TestNarrative:
    def test_every_loser_has_explicit_reason(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        ruled_out = r["_elimination_narrative"]["ruled_out"]
        assert len(ruled_out) == 3
        for entry in ruled_out:
            assert entry["reason"]
            assert "lower net support" in entry["reason"]

    def test_counterfactual_names_missing_evidence(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        assert r["_counterfactual"].startswith(
            "Evidence most likely to change this conclusion:")
        assert "cap:" in r["_counterfactual"]

    def test_false_lead_overlap_refutes_leader(self, monkeypatch):
        # critique gap tokens overlap the leader's name
        res = _result()
        res["_critique"] = {"gaps": ["checkout memory limit review"]}
        r = _run(result=res, monkeypatch=monkeypatch)
        winner = [h for h in r["_hypothesis_graph"]["hypotheses"]
                  if h["status"] == "confirmed"][0]
        reasons = [e["reason"] for e in winner["refuting_evidence"]]
        assert "known_false_lead_overlap" in reasons

    def test_narrative_json_safe(self, monkeypatch):
        r = _run(monkeypatch=monkeypatch)
        payload = {"g": r["_hypothesis_graph"],
                    "n": r["_elimination_narrative"],
                    "c": r["_counterfactual"]}
        assert payload == json.loads(json.dumps(payload))
