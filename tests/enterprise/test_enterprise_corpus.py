"""Synthetic Enterprise Validation Platform — corpus + validator tests.

Verifies the corpus is deterministic, content-addressed, spans the declared
enterprise tool sources, is well-formed (ground truth + operator expectations),
matches the committed file, and that the reused EIC scorer grades a perfect
submission high and a wrong one low — with NOT_MEASURED when no engine runs.
This is synthetic validation data; no engine result is fabricated.
"""
from __future__ import annotations

import json
import os

from eval.enterprise.build_corpus import (
    TOOL_SOURCES, build_corpus,
)
from eval.enterprise.validate import check_expected, load_corpus, validate
from sentinel_core.eic import make_submission

_CORPUS_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "eval", "enterprise",
    "corpus.json")


class TestCorpus:
    def test_deterministic_rebuild(self):
        a = json.dumps(build_corpus(), sort_keys=True)
        b = json.dumps(build_corpus(), sort_keys=True)
        assert a == b

    def test_content_addressed_task_hashes(self):
        for e in build_corpus()["corpus"]:
            assert len(e["task"]["task_hash"]) == 16

    def test_committed_file_matches_build(self):
        # guards against corpus.json drifting from the builder
        with open(_CORPUS_PATH) as f:
            committed = json.load(f)
        assert committed == build_corpus()

    def test_all_tool_sources_exercised(self):
        c = build_corpus()
        assert set(c["tool_sources_exercised"]) == set(TOOL_SOURCES)

    def test_every_task_well_formed(self):
        for e in build_corpus()["corpus"]:
            gt = e["task"]["ground_truth"]
            assert gt["root_cause"] and gt["root_cause_service"]
            assert gt["necessary_evidence"] and gt["decisive_evidence"]
            exp = e["expected"]
            assert exp["owner"] and exp["recommendation"]
            assert 0 <= exp["confidence_min"] <= exp["confidence_max"] <= 100

    def test_json_safe(self):
        c = build_corpus()
        assert c == json.loads(json.dumps(c))


class TestValidator:
    def test_no_submissions_is_not_measured(self):
        r = validate({})
        assert r["status"] == "NOT_MEASURED"
        assert r["tasks"] == 8

    def test_perfect_submission_scores_high(self):
        corpus = build_corpus()
        subs = {}
        for e in corpus["corpus"]:
            t, exp = e["task"], e["expected"]
            gt = t["ground_truth"]
            sub = make_submission(
                engine="oracle", task_id=t["task_id"],
                root_cause=gt["root_cause"],
                localized_service=gt["root_cause_service"],
                evidence_used=gt["necessary_evidence"],
                decisive_evidence=gt["decisive_evidence"],
                hypotheses=[gt["root_cause"]],
                ruled_out=t["traps"]["false_hypotheses"],
                proof="evidence shows " + gt["root_cause"],
                confidence=(exp["confidence_min"] + exp["confidence_max"]) // 2)
            sub["owner"] = exp["owner"]
            sub["recommendation"] = exp["recommendation"]
            subs[t["task_id"]] = sub
        r = validate(subs, corpus)
        assert r["status"] == "measured" and r["graded"] == 8
        # oracle answers score at the top of the leaderboard
        assert r["leaderboard"]["engines"][0]["eic_score_mean"] > 0.8
        # operator-facing checks pass for the oracle
        for pt in r["per_task"]:
            c = pt["expected_checks"]
            assert c["owner_ok"] and c["confidence_in_range"] and c["recommendation_match"]

    def test_wrong_submission_scores_low(self):
        corpus = build_corpus()
        subs = {}
        for e in corpus["corpus"]:
            t = e["task"]
            subs[t["task_id"]] = make_submission(
                engine="weak", task_id=t["task_id"],
                root_cause="unrelated cause", localized_service="wrong-service",
                evidence_used=["thousandeyes_probe"], confidence=99)
        r = validate(subs, corpus)
        assert r["leaderboard"]["engines"][0]["eic_score_mean"] < 0.5

    def test_check_expected_not_measured_when_field_absent(self):
        exp = {"owner": "x-team", "confidence_min": 60, "confidence_max": 90,
               "recommendation": "do the thing"}
        # submission omits owner/recommendation -> those checks are None
        c = check_expected(exp, {"confidence": 70})
        assert c["owner_ok"] is None
        assert c["recommendation_match"] is None
        assert c["confidence_in_range"] is True

    def test_no_new_evaluation_framework_imported(self):
        # the platform reuses the existing EIC benchmark, defines no new scorer
        import inspect
        import eval.enterprise.validate as v
        src = inspect.getsource(v)
        assert "score_submission" in src and "leaderboard" in src
        assert "def score_submission" not in src   # reused, not redefined
