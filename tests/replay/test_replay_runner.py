"""SentinelReplay — ReplayRunner + ReplayStore + report tests."""
from __future__ import annotations

import copy
import json

import pytest

from tests.replay.replay_runner import ReplayRunner
from tests.replay.replay_store import ReplayStore, ReplayStoreError
from tests.replay.replay_report import (
    render_master_report,
    render_regression_report,
    render_replay_report,
    render_trend_report,
    to_json,
)
from tests.replay.schemas import BenchmarkRun, ReplayResult, Verdict


# ---------------------------------------------------------------------------
# ReplayStore
# ---------------------------------------------------------------------------

class TestReplayStore:
    def test_empty_store_lists_nothing(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        assert s.list_runs() == ()

    def test_save_and_load(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        run = runner.capture_run(run_id="r1", generated_at="2026-07-01T00:00:00Z")
        assert s.has("r1")
        loaded = s.load("r1")
        assert loaded.run_id == "r1"
        assert loaded.generated_at == "2026-07-01T00:00:00Z"
        assert len(loaded.scorecards) == 5

    def test_load_all_sorted(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="rb", generated_at="2026-07-02T00:00:00Z")
        runner.capture_run(run_id="ra", generated_at="2026-07-01T00:00:00Z")
        ids = s.list_runs()
        assert ids == ("ra", "rb")

    def test_invalid_run_id_rejected(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        with pytest.raises(ReplayStoreError):
            s.load("")
        with pytest.raises(ReplayStoreError):
            s.load("has/slash")

    def test_missing_run_raises(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        with pytest.raises(ReplayStoreError):
            s.load("does_not_exist")

    def test_date_range_filter(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="r1", generated_at="2026-06-01T00:00:00Z")
        runner.capture_run(run_id="r2", generated_at="2026-07-01T00:00:00Z")
        runner.capture_run(run_id="r3", generated_at="2026-08-01T00:00:00Z")
        got = s.load_in_date_range("2026-06-15T00:00:00Z", "2026-07-15T00:00:00Z")
        assert tuple(r.run_id for r in got) == ("r2",)

    def test_service_and_incident_type_filter(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(
            run_id="r1", generated_at="",
            metadata={"service": "checkout", "incident_type": "saturation"},
        )
        runner.capture_run(
            run_id="r2", generated_at="",
            metadata={"service": "payments", "incident_type": "network"},
        )
        assert tuple(r.run_id for r in s.load_by_service("checkout")) == ("r1",)
        assert tuple(r.run_id for r in s.load_by_incident_type("network")) == ("r2",)

    def test_deterministic_save(self, tmp_path):
        """Persisted JSON must be sort_keys deterministic for the same
        run captured twice."""
        s = ReplayStore(tmp_path / "runs")
        r1 = ReplayRunner(store=s).capture_run(
            run_id="det1", generated_at="2026-07-01T00:00:00Z"
        )
        path = s._path_for("det1")
        bytes1 = path.read_bytes()
        # Re-save the same object; content should not change
        s.save(r1)
        bytes2 = path.read_bytes()
        assert bytes1 == bytes2


# ---------------------------------------------------------------------------
# ReplayRunner — replay_scenario / replay_corpus
# ---------------------------------------------------------------------------

class TestReplayRunner:
    def test_replay_scenario_no_baseline_is_new(self):
        r = ReplayRunner().replay_scenario("k8s_pod_crashloop")
        assert r.verdict == Verdict.NEW.value
        assert r.baseline is None
        assert r.current.overall_score == 1.0

    def test_replay_scenario_with_baseline_stable(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="baseline", generated_at="2026-07-01T00:00:00Z")
        r = runner.replay_scenario(
            "k8s_pod_crashloop", baseline_run_id="baseline"
        )
        assert r.verdict == Verdict.STABLE.value
        assert r.delta["overall_score"] == 0.0

    def test_replay_scenario_regressed(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="baseline", generated_at="")
        # Feed a degraded external output
        r = runner.replay_scenario(
            "k8s_pod_crashloop",
            investigation_output={
                "root_cause": "wrong",
                "confidence": 0,
                "evidence_keys": [],
                "decision_signals": [],
                "mtti_ms": 9999999,
                "runtime_cost": 9999,
            },
            baseline_run_id="baseline",
        )
        assert r.verdict == Verdict.REGRESSED.value

    def test_replay_scenario_improved(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        # Poor baseline
        outputs = {
            "k8s_pod_crashloop": {
                "root_cause": "wrong",
                "confidence": 0,
                "evidence_keys": [],
                "decision_signals": [],
                "mtti_ms": 9999999,
                "runtime_cost": 9999,
            },
        }
        runner.capture_run(run_id="baseline", generated_at="",
                            investigation_outputs=outputs)
        # Replay with the perfect mock output
        r = runner.replay_scenario(
            "k8s_pod_crashloop", baseline_run_id="baseline"
        )
        assert r.verdict == Verdict.IMPROVED.value
        assert r.delta["overall_score"] > 0.05

    def test_replay_corpus_all_new_no_baseline(self):
        results = ReplayRunner().replay_corpus()
        assert len(results) == 5
        for r in results:
            assert r.verdict == Verdict.NEW.value

    def test_replay_corpus_service_filter(self):
        results = ReplayRunner().replay_corpus(filter_service="checkout")
        # 4 of the 5 scenarios target service "checkout" (all except auth)
        assert len(results) == 4
        for r in results:
            assert r.current.overall_score == 1.0

    def test_replay_corpus_incident_type_filter(self):
        results = ReplayRunner().replay_corpus(filter_incident_type="network")
        assert len(results) == 1
        assert results[0].scenario_id == "dns_resolution_failure"

    def test_replay_corpus_tag_filter(self):
        results = ReplayRunner().replay_corpus(filter_tags=("dns",))
        assert len(results) == 1

    def test_compare_runs(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="a", generated_at="")
        runner.capture_run(run_id="b", generated_at="")
        cmp_ = runner.compare_runs("a", "b")
        assert cmp_["run_a"] == "a"
        assert cmp_["run_b"] == "b"
        assert len(cmp_["per_scenario"]) == 5
        for row in cmp_["per_scenario"]:
            assert row["overall_score"]["delta"] == 0.0


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestReports:
    def test_replay_report_shape(self):
        results = ReplayRunner().replay_corpus()
        rep = render_replay_report(results)
        assert rep["result_count"] == 5
        assert rep["verdicts"].get(Verdict.NEW.value) == 5

    def test_deterministic_replay_report_json(self):
        results = ReplayRunner().replay_corpus()
        s1 = to_json(render_replay_report(results))
        s2 = to_json(render_replay_report(results))
        assert s1 == s2

    def test_trend_report_over_multiple_runs(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="r1", generated_at="2026-07-01T00:00:00Z")
        runner.capture_run(run_id="r2", generated_at="2026-07-02T00:00:00Z")
        runs = s.load_all()
        rep = render_trend_report(runs)
        assert rep["run_count"] == 2
        assert len(rep["timeline"]) == 2
        assert "root_cause_match" in rep["trends"]

    def test_regression_report_flags_drops(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        # First good run
        runner.capture_run(run_id="r1", generated_at="2026-07-01T00:00:00Z")
        # Second run with a degraded scenario
        outputs = {
            "k8s_pod_crashloop": {
                "root_cause": "wrong", "confidence": 0,
                "evidence_keys": [], "decision_signals": [],
                "mtti_ms": 9999999, "runtime_cost": 9999,
            }
        }
        runner.capture_run(run_id="r2", generated_at="2026-07-02T00:00:00Z",
                            investigation_outputs=outputs)
        runs = s.load_all()
        rep = render_regression_report(runs, threshold=0.05)
        # Should flag at least one dimension regression
        assert len(rep["regressions"]) >= 1

    def test_master_report(self, tmp_path):
        s = ReplayStore(tmp_path / "runs")
        runner = ReplayRunner(store=s)
        runner.capture_run(run_id="r1", generated_at="2026-07-01T00:00:00Z")
        runs = s.load_all()
        results = runner.replay_corpus()
        rep = render_master_report(runs, results)
        for key in ("replay_report", "trend_report", "regression_report",
                     "heatmap_report", "learning_report",
                     "recommendations_report", "weakness_leaderboard"):
            assert key in rep
        # JSON round-trip
        json.loads(to_json(rep))


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_no_network_imports(self):
        for mod_name in ("tests.replay.replay_runner",
                          "tests.replay.replay_store",
                          "tests.replay.replay_report",
                          "tests.replay.trend_analysis",
                          "tests.replay.learning_engine",
                          "tests.replay.recommendation_engine",
                          "tests.replay.heatmap",
                          "tests.replay.schemas"):
            import importlib
            m = importlib.import_module(mod_name)
            src = open(m.__file__).read()
            for banned in ("requests", "httpx", "urllib3", "boto3",
                             "openai", "anthropic", "kubernetes"):
                assert banned not in src, f"{mod_name} imports {banned}"

    def test_does_not_import_supervisor_agent(self):
        import importlib
        for mod_name in ("tests.replay.replay_runner",
                          "tests.replay.learning_engine",
                          "tests.replay.recommendation_engine",
                          "tests.replay.trend_analysis"):
            m = importlib.import_module(mod_name)
            src = open(m.__file__).read()
            assert "supervisor.agent" not in src
