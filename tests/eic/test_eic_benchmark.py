"""Enterprise Investigation Challenge (EIC) — engine-agnostic benchmark tests.

Coverage: task/submission schemas (deterministic ids, normalization), the
scorer's ten dimensions (correct vs weak engine), engine-agnosticism (no
SentinelAI internals in task/scorer), the SentinelAI boundary adapter,
leaderboard ranking + per-category/difficulty breakdown, determinism, and
that the seed task corpus loads and scores.
"""
from __future__ import annotations

import glob
import json
import os

from sentinel_core.eic import (
    CATEGORIES,
    DIFFICULTY,
    leaderboard,
    make_submission,
    make_task,
    score_submission,
)
from sentinel_core.eic.adapter import sentinelai_submission
from sentinel_core.investigation_value.scientific_validation import NOT_MEASURED


def _task():
    return make_task(
        task_id="EIC-DB-001", category="database",
        difficulty="competing_hypotheses",
        incident={"summary": "checkout 5xx", "service": "checkout"},
        telemetry={"db_pool_metrics": {"active": 100, "max": 100},
                   "app_logs": {"errors": ["pool timeout"]},
                   "dns_probe": {"ok": True}},
        ground_truth={"root_cause": "database connection pool exhaustion",
                      "root_cause_keywords": ["connection pool", "exhaustion"],
                      "root_cause_service": "db",
                      "necessary_evidence": ["db_pool_metrics", "app_logs"],
                      "decisive_evidence": ["db_pool_metrics"]},
        traps={"distractor_evidence": ["dns_probe"],
               "false_hypotheses": ["dns failure", "bad deployment"]})


def _strong():
    return make_submission(
        engine="engine_a", task_id="EIC-DB-001",
        root_cause="database connection pool exhaustion", localized_service="db",
        hypotheses=["database connection pool exhaustion", "dns failure",
                    "bad deployment"],
        ruled_out=["dns failure", "bad deployment"],
        evidence_used=["db_pool_metrics", "app_logs"],
        decisive_evidence=["db_pool_metrics"], confidence=85,
        proof="db_pool_metrics show active=max", replay_hash="rh1")


def _weak():
    return make_submission(
        engine="engine_b", task_id="EIC-DB-001",
        root_cause="DNS resolution failure", localized_service="checkout",
        hypotheses=["dns failure"], ruled_out=[],
        evidence_used=["dns_probe", "app_logs"], decisive_evidence=[],
        confidence=70, proof="", replay_hash="")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_task_deterministic_hash(self):
        assert _task()["task_hash"] == _task()["task_hash"]

    def test_task_is_engine_agnostic(self):
        # no SentinelAI-internal underscore keys anywhere in the task
        t = _task()
        assert all(not k.startswith("_") for k in t)
        assert set(("ground_truth", "traps", "telemetry")) <= set(t)

    def test_categories_and_difficulties_defined(self):
        assert "database" in CATEGORIES and "kubernetes" in CATEGORIES
        assert "competing_hypotheses" in DIFFICULTY

    def test_submission_normalizes(self):
        s = make_submission(engine="x", task_id="t", root_cause="rc",
                            hypotheses=("a", "b"))
        assert s["hypotheses"] == ["a", "b"]
        assert s["confidence"] == 0


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class TestScorer:
    def test_strong_engine_high_score(self):
        s = score_submission(_task(), _strong())
        d = s["dimensions"]
        assert d["rca_correctness"] == 1.0
        assert d["localization"] == 1.0
        assert d["false_lead_avoidance"] == 1.0
        assert d["decisive_evidence_latency"] == 1.0
        assert d["distractor_avoidance"] == 1.0
        assert s["eic_score"] > 0.9

    def test_weak_engine_low_score(self):
        s = score_submission(_task(), _weak())
        d = s["dimensions"]
        assert d["rca_correctness"] == 0.0
        assert d["distractor_avoidance"] == 0.0     # collected the dns_probe trap
        assert d["decisive_evidence_latency"] == 0.0  # never collected decisive
        assert s["eic_score"] < 0.2

    def test_strong_beats_weak(self):
        assert score_submission(_task(), _strong())["eic_score"] > \
            score_submission(_task(), _weak())["eic_score"]

    def test_missing_dimensions_not_measured(self):
        # a task with no traps => false-lead / distractor dims NOT_MEASURED
        t = make_task(task_id="t", category="dns", difficulty="single_cause",
                      incident={"service": "x"}, telemetry={"a": {}},
                      ground_truth={"root_cause": "dns", "root_cause_keywords":
                                    ["dns"], "necessary_evidence": ["a"]})
        s = score_submission(t, make_submission(
            engine="e", task_id="t", root_cause="dns", evidence_used=["a"]))
        assert s["dimensions"]["false_lead_avoidance"] == NOT_MEASURED
        assert s["dimensions"]["distractor_avoidance"] == NOT_MEASURED

    def test_deterministic(self):
        a = json.dumps(score_submission(_task(), _strong()), sort_keys=True)
        b = json.dumps(score_submission(_task(), _strong()), sort_keys=True)
        assert a == b


# ---------------------------------------------------------------------------
# SentinelAI adapter (boundary)
# ---------------------------------------------------------------------------

class TestAdapter:
    def test_result_to_submission(self):
        result = {"root_cause": "database connection pool exhaustion",
                  "confidence": 85, "reasoning": "db_pool_metrics show max",
                  "_hypothesis_graph": {"hypotheses": [
                      {"name": "database connection pool exhaustion"}]},
                  "_elimination_narrative": {"ruled_out": [
                      {"name": "dns failure"}]},
                  "_decision_intelligence": {"evidence_attribution": {
                      "decisive_evidence": ["db_pool_metrics"]}},
                  "_causal_investigation": {"localization": {
                      "root_cause_service": "db"}},
                  "_evidence_snapshot": {"db_pool_metrics": True,
                                          "app_logs": True}}
        sub = sentinelai_submission(
            result, task_id="EIC-DB-001",
            evidence_sequence=["db_pool_metrics", "app_logs"], replay_hash="r")
        assert sub["engine"] == "sentinelai"
        assert sub["localized_service"] == "db"
        s = score_submission(_task(), sub)
        assert s["eic_score"] > 0.9

    def test_shadow_off_still_valid(self):
        base = {"root_cause": "x", "confidence": 50, "reasoning": "y"}
        sub = sentinelai_submission(base, task_id="t")
        assert sub["engine"] == "sentinelai"
        assert sub["hypotheses"] == []
        assert sub["localized_service"] == ""


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

class TestLeaderboard:
    def test_ranks_by_score(self):
        lb = leaderboard([score_submission(_task(), _strong()),
                          score_submission(_task(), _weak())],
                         release="2026.01")
        assert lb["leader"] == "engine_a"
        assert lb["engines"][0]["engine"] == "engine_a"
        assert lb["engines"][0]["eic_score_mean"] > \
            lb["engines"][1]["eic_score_mean"]

    def test_per_category_breakdown(self):
        lb = leaderboard([score_submission(_task(), _strong())])
        row = lb["engines"][0]
        assert "database" in row["by_category"]
        assert "competing_hypotheses" in row["by_difficulty"]

    def test_json_safe(self):
        lb = leaderboard([score_submission(_task(), _strong())])
        assert lb == json.loads(json.dumps(lb))


# ---------------------------------------------------------------------------
# Seed corpus
# ---------------------------------------------------------------------------

class TestSeedCorpus:
    def test_seed_tasks_load_and_score(self):
        root = os.path.join(os.getcwd(), "eval", "eic", "tasks")
        files = sorted(glob.glob(os.path.join(root, "EIC-*.json")))
        assert len(files) >= 5
        cats = set()
        for f in files:
            task = json.load(open(f))
            cats.add(task["category"])
            # a perfect oracle submission must score highly on every seed task
            gt = task["ground_truth"]
            oracle = make_submission(
                engine="oracle", task_id=task["task_id"],
                root_cause=gt["root_cause"],
                localized_service=gt["root_cause_service"],
                hypotheses=[gt["root_cause"]],
                ruled_out=task["traps"]["false_hypotheses"],
                evidence_used=gt["necessary_evidence"],
                decisive_evidence=gt["decisive_evidence"],
                confidence=90, proof=" ".join(task["telemetry_keys"]),
                replay_hash="r")
            s = score_submission(task, oracle)
            assert s["eic_score"] > 0.85, f"{task['task_id']} -> {s['eic_score']}"
        assert len(cats) >= 4                 # multiple categories represented
