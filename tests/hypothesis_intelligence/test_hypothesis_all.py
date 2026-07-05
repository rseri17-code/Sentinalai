"""Hypothesis Intelligence — comprehensive tests across all modules.

One file with focused test classes per module keeps discovery fast
and imports minimal.
"""
from __future__ import annotations

import json

import pytest

from sentinel_core.hypotheses import (
    Hypothesis,
    HypothesisEvidence,
    HypothesisGraph,
    HypothesisScore,
    HypothesisStatus,
    HypothesisTracker,
    HypothesisTransition,
    make_hypothesis_id,
    render_hypothesis_report,
    render_master_report,
    render_ruled_out_report,
    render_scored_report,
    render_summary_report,
    score_hypothesis,
    score_hypothesis_graph,
    to_json,
)


# ---------------------------------------------------------------------------
# schemas.py
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_id_deterministic(self):
        assert make_hypothesis_id("db_pool") == make_hypothesis_id("db_pool")
        assert make_hypothesis_id("db_pool") != make_hypothesis_id("dns")

    def test_hypothesis_make_defaults(self):
        h = Hypothesis.make("db pool exhausted")
        assert h.status == HypothesisStatus.PROPOSED.value
        assert h.confidence == 50
        assert h.hypothesis_id.startswith("hyp:")

    def test_hypothesis_frozen(self):
        h = Hypothesis.make("x")
        with pytest.raises(Exception):
            h.status = "confirmed"

    def test_hypothesis_evidence_default_supports(self):
        e = HypothesisEvidence(key="logs")
        assert e.supports is True
        assert e.weight == 1.0

    def test_to_dict_json_safe(self):
        h = Hypothesis.make("x", supporting_evidence=(HypothesisEvidence(key="k"),))
        d = h.to_dict()
        # tuples → lists
        assert isinstance(d["supporting_evidence"], list)
        json.dumps(d)

    def test_transition_dataclass(self):
        t = HypothesisTransition(at="now", from_status="proposed",
                                    to_status="supported",
                                    confidence_before=50, confidence_after=70)
        assert t.confidence_after == 70

    def test_is_terminal(self):
        h1 = Hypothesis.make("a", status=HypothesisStatus.CONFIRMED.value)
        h2 = Hypothesis.make("b", status=HypothesisStatus.RULED_OUT.value)
        h3 = Hypothesis.make("c", status=HypothesisStatus.PROPOSED.value)
        assert h1.is_terminal()
        assert h2.is_terminal()
        assert not h3.is_terminal()


# ---------------------------------------------------------------------------
# hypothesis_graph.py
# ---------------------------------------------------------------------------

class TestHypothesisGraph:
    def test_empty(self):
        g = HypothesisGraph()
        assert g.count() == 0
        assert g.confirmed() == ()
        assert g.ruled_out() == ()

    def test_get_by_id(self):
        h = Hypothesis.make("x")
        g = HypothesisGraph(hypotheses=(h,))
        assert g.get(h.hypothesis_id) is h
        assert g.get("nope") is None

    def test_by_status_helpers(self):
        h1 = Hypothesis.make("a", status=HypothesisStatus.SUPPORTED.value)
        h2 = Hypothesis.make("b", status=HypothesisStatus.REFUTED.value)
        h3 = Hypothesis.make("c", status=HypothesisStatus.CONFIRMED.value)
        g = HypothesisGraph(hypotheses=(h1, h2, h3))
        assert g.supported() == (h1,)
        assert g.refuted() == (h2,)
        assert g.confirmed() == (h3,)

    def test_to_dict_deterministic(self):
        h1 = Hypothesis.make("b")
        h2 = Hypothesis.make("a")
        g = HypothesisGraph(hypotheses=(h1, h2))
        d = g.to_dict()
        ids = [h["hypothesis_id"] for h in d["hypotheses"]]
        assert ids == sorted(ids)

    def test_frozen(self):
        g = HypothesisGraph()
        with pytest.raises(Exception):
            g.investigation_id = "x"


# ---------------------------------------------------------------------------
# hypothesis_tracker.py
# ---------------------------------------------------------------------------

class TestHypothesisTracker:
    def test_propose_is_idempotent(self):
        t = HypothesisTracker()
        h1 = t.propose("db pool")
        h2 = t.propose("db pool")
        assert h1.hypothesis_id == h2.hypothesis_id
        assert t.build_graph().count() == 1

    def test_add_evidence(self):
        t = HypothesisTracker()
        h = t.propose("db pool")
        t.add_supporting_evidence(h.hypothesis_id, "logs", weight=0.8)
        t.add_refuting_evidence(h.hypothesis_id, "metrics_ok", weight=0.4)
        g = t.build_graph()
        updated = g.get(h.hypothesis_id)
        assert len(updated.supporting_evidence) == 1
        assert len(updated.refuting_evidence) == 1

    def test_rule_out_populates_reason(self):
        t = HypothesisTracker()
        h = t.propose("dns")
        t.rule_out(h.hypothesis_id, reason="coredns healthy in metrics")
        updated = t.build_graph().get(h.hypothesis_id)
        assert updated.status == HypothesisStatus.RULED_OUT.value
        assert "coredns" in updated.ruled_out_reason
        assert updated.confidence == 0

    def test_confirm_captures_root_cause(self):
        t = HypothesisTracker()
        h = t.propose("db pool")
        t.confirm(h.hypothesis_id, root_cause="pool at 100/100",
                    reason="metrics + trace both show 100/100",
                    confidence=95, mtti_contribution_ms=45000)
        updated = t.build_graph().get(h.hypothesis_id)
        assert updated.status == HypothesisStatus.CONFIRMED.value
        assert updated.root_cause == "pool at 100/100"
        assert updated.confidence == 95
        assert updated.mtti_contribution_ms == 45000

    def test_transition_records_history(self):
        t = HypothesisTracker()
        h = t.propose("db pool", initial_confidence=40)
        t.transition(h.hypothesis_id, HypothesisStatus.SUPPORTED,
                       new_confidence=70, reason="logs")
        t.transition(h.hypothesis_id, HypothesisStatus.CONFIRMED,
                       new_confidence=90, reason="trace")
        updated = t.build_graph().get(h.hypothesis_id)
        assert len(updated.transitions) == 2
        assert updated.transitions[-1].confidence_after == 90

    def test_unknown_id_raises(self):
        t = HypothesisTracker()
        with pytest.raises(KeyError):
            t.transition("nope", HypothesisStatus.SUPPORTED)


# ---------------------------------------------------------------------------
# scoring.py
# ---------------------------------------------------------------------------

class TestScoring:
    def test_score_deterministic(self):
        h = Hypothesis.make("x",
                              supporting_evidence=(HypothesisEvidence(key="a", weight=0.7),))
        s1 = score_hypothesis(h)
        s2 = score_hypothesis(h)
        assert s1 == s2

    def test_net_score_reflects_evidence(self):
        h = Hypothesis.make("x",
                              supporting_evidence=(HypothesisEvidence(key="a", weight=0.9),),
                              refuting_evidence=(HypothesisEvidence(key="b", weight=0.4),))
        s = score_hypothesis(h)
        assert s.support_score == 0.9
        assert s.refute_score == 0.4
        assert s.net_score == 0.5

    def test_confidence_delta_from_transitions(self):
        h = Hypothesis.make("x",
                              transitions=(HypothesisTransition(
                                  at="", from_status="proposed", to_status="supported",
                                  confidence_before=50, confidence_after=70,
                              ),
                              HypothesisTransition(
                                  at="", from_status="supported", to_status="confirmed",
                                  confidence_before=70, confidence_after=95,
                              )))
        s = score_hypothesis(h)
        assert s.confidence_delta == 45

    def test_score_graph_sorted(self):
        g = HypothesisGraph(hypotheses=(
            Hypothesis.make("b"), Hypothesis.make("a"),
        ))
        scores = score_hypothesis_graph(g)
        ids = [s.hypothesis_id for s in scores]
        assert ids == sorted(ids)

    def test_score_to_dict_json_safe(self):
        h = Hypothesis.make("x")
        s = score_hypothesis(h)
        json.dumps(s.to_dict())


# ---------------------------------------------------------------------------
# report.py
# ---------------------------------------------------------------------------

class TestReports:
    def _built_graph(self) -> HypothesisGraph:
        t = HypothesisTracker(investigation_id="inv-1",
                                started_at="2026-07-01T00:00:00Z")
        a = t.propose("db pool", initial_confidence=40)
        t.add_supporting_evidence(a.hypothesis_id, "logs", weight=0.8)
        t.confirm(a.hypothesis_id, root_cause="pool at 100/100",
                    reason="metrics confirm", confidence=95,
                    mtti_contribution_ms=42000)
        b = t.propose("dns")
        t.add_refuting_evidence(b.hypothesis_id, "coredns_healthy",
                                  weight=0.9)
        t.rule_out(b.hypothesis_id, reason="coredns metrics healthy")
        t.finalise(completed_at="2026-07-01T00:05:00Z")
        return t.build_graph()

    def test_hypothesis_report(self):
        r = render_hypothesis_report(self._built_graph())
        assert r["graph"]["count"] == 2

    def test_summary_report(self):
        r = render_summary_report(self._built_graph())
        assert r["hypothesis_count"] == 2
        assert r["confirmed_count"] == 1
        assert r["ruled_out_count"] == 1
        assert "pool at 100/100" in r["confirmed_root_causes"]
        assert r["total_mtti_contribution_ms"] == 42000

    def test_ruled_out_report(self):
        r = render_ruled_out_report(self._built_graph())
        assert len(r["ruled_out"]) == 1
        assert "coredns" in r["ruled_out"][0]["ruled_out_reason"]

    def test_scored_report(self):
        r = render_scored_report(self._built_graph())
        assert len(r["scores"]) == 2

    def test_master_report_deterministic(self):
        g = self._built_graph()
        j1 = to_json(render_master_report(g))
        j2 = to_json(render_master_report(g))
        assert j1 == j2
        json.loads(j1)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_no_forbidden_imports(self):
        import importlib
        for name in ("sentinel_core.hypotheses.schemas",
                      "sentinel_core.hypotheses.hypothesis_graph",
                      "sentinel_core.hypotheses.hypothesis_tracker",
                      "sentinel_core.hypotheses.scoring",
                      "sentinel_core.hypotheses.report"):
            src = open(importlib.import_module(name).__file__).read()
            for banned in ("requests", "httpx", "urllib3", "boto3",
                             "openai", "anthropic", "kubernetes",
                             "supervisor.agent"):
                assert banned not in src, f"{name} imports {banned}"
