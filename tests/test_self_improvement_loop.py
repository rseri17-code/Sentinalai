"""Tests for supervisor/self_improvement_loop.py."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from supervisor.self_improvement_loop import (
    EvalCorpusScore,
    Experiment,
    ImprovementReport,
    SelfImprovementLoop,
    run_improvement_cycle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_experiences(n: int = 10, incident_type: str = "error_spike", quality: float = 0.55) -> list[dict]:
    return [
        {
            "incident_id": f"INC{i:05d}",
            "incident_type": incident_type,
            "service": "svc-a",
            "root_cause": "cpu spike",
            "evidence_keys": ["check_golden_signals", "search_error_logs"],
            "confidence": 80,
            "online_quality_score": quality,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        for i in range(n)
    ]


def _make_strategy(incident_type: str = "error_spike") -> dict:
    return {
        incident_type: {
            "query_metrics": {
                "weight": 0.8,
                "calls": 50,
                "ema_signal": 1.2,      # underweighted relative to signal
                "last_updated": "2026-01-01T00:00:00+00:00",
            },
            "check_golden_signals": {
                "weight": 1.5,
                "calls": 100,
                "ema_signal": 1.0,
                "last_updated": "2026-01-01T00:00:00+00:00",
            },
        }
    }


@pytest.fixture()
def tmploop(tmp_path):
    exp_path = str(tmp_path / "experience_store.json")
    strat_path = str(tmp_path / "evolved_strategy.json")
    report_path = str(tmp_path / "improvement_reports.json")
    return SelfImprovementLoop(exp_path, strat_path, report_path), tmp_path, exp_path, strat_path, report_path


# ---------------------------------------------------------------------------
# EvalCorpusScore / _score_corpus
# ---------------------------------------------------------------------------

class TestScoreCorpus:
    def test_empty_experiences_returns_zero(self, tmploop):
        loop, *_ = tmploop
        score = loop._score_corpus([])
        assert score.overall == 0.0
        assert score.total_count == 0
        assert score.failure_count == 0
        assert score.worst_incident_type == ""

    def test_single_type_mean_quality(self, tmploop):
        loop, *_ = tmploop
        exps = _make_experiences(10, quality=0.70)
        score = loop._score_corpus(exps)
        assert score.overall == pytest.approx(0.70, abs=1e-6)
        assert score.total_count == 10
        assert score.failure_count == 0  # 0.70 >= 0.60

    def test_failures_counted_below_threshold(self, tmploop):
        loop, *_ = tmploop
        exps = _make_experiences(5, quality=0.55) + _make_experiences(5, quality=0.80)
        score = loop._score_corpus(exps)
        assert score.failure_count == 5

    def test_worst_incident_type_identified(self, tmploop):
        loop, *_ = tmploop
        exps = (
            _make_experiences(5, incident_type="error_spike", quality=0.80)
            + _make_experiences(5, incident_type="timeout", quality=0.45)
        )
        score = loop._score_corpus(exps)
        assert score.worst_incident_type == "timeout"

    def test_per_incident_type_keys_present(self, tmploop):
        loop, *_ = tmploop
        exps = (
            _make_experiences(3, incident_type="latency", quality=0.70)
            + _make_experiences(3, incident_type="network", quality=0.60)
        )
        score = loop._score_corpus(exps)
        assert "latency" in score.per_incident_type
        assert "network" in score.per_incident_type


# ---------------------------------------------------------------------------
# Experiment simulation
# ---------------------------------------------------------------------------

class TestRunExperiment:
    def test_no_pool_returns_zero_delta(self, tmploop):
        loop, *_ = tmploop
        # All experiences already use query_metrics
        exps = [
            {**_make_experiences(1)[0], "evidence_keys": ["query_metrics", "check_golden_signals"]}
        ]
        strategy = _make_strategy()
        step_data = strategy["error_spike"]["query_metrics"]
        exp = loop._run_experiment(
            target_type="error_spike",
            step_name="query_metrics",
            step_data=step_data,
            experiences=exps,
            working_strategy=strategy,
            min_delta=0.02,
        )
        assert exp.affected_experiences == 0
        assert exp.delta == 0.0
        assert not exp.accepted

    def test_high_ema_signal_produces_positive_delta(self, tmploop):
        loop, *_ = tmploop
        exps = _make_experiences(20, quality=0.55)
        strategy = _make_strategy()
        step_data = {"weight": 0.8, "calls": 50, "ema_signal": 1.5, "last_updated": ""}
        exp = loop._run_experiment(
            target_type="error_spike",
            step_name="query_metrics",  # not in evidence_keys
            step_data=step_data,
            experiences=exps,
            working_strategy=strategy,
            min_delta=0.02,
        )
        assert exp.affected_experiences == 20
        assert exp.delta > 0.0

    def test_experiment_accepted_when_delta_meets_threshold(self, tmploop):
        loop, *_ = tmploop
        exps = _make_experiences(20, quality=0.50)
        strategy = _make_strategy()
        step_data = {"weight": 0.5, "calls": 10, "ema_signal": 2.0, "last_updated": ""}
        exp = loop._run_experiment(
            target_type="error_spike",
            step_name="query_metrics",
            step_data=step_data,
            experiences=exps,
            working_strategy=strategy,
            min_delta=0.02,
        )
        assert exp.accepted

    def test_experiment_rejected_when_delta_below_threshold(self, tmploop):
        loop, *_ = tmploop
        exps = _make_experiences(5, quality=0.90)  # already high quality → tiny delta
        strategy = _make_strategy()
        step_data = {"weight": 1.4, "calls": 5, "ema_signal": 0.1, "last_updated": ""}
        exp = loop._run_experiment(
            target_type="error_spike",
            step_name="query_metrics",
            step_data=step_data,
            experiences=exps,
            working_strategy=strategy,
            min_delta=0.10,  # high threshold
        )
        assert not exp.accepted

    def test_weight_after_capped_at_max(self, tmploop):
        loop, *_ = tmploop
        step_data = {"weight": 2.45, "calls": 5, "ema_signal": 1.0, "last_updated": ""}
        exps = _make_experiences(5, quality=0.50)
        strategy = _make_strategy()
        exp = loop._run_experiment(
            target_type="error_spike",
            step_name="query_metrics",
            step_data=step_data,
            experiences=exps,
            working_strategy=strategy,
            min_delta=0.0,
        )
        assert exp.weight_after <= 2.5

    def test_candidate_quality_never_exceeds_one(self, tmploop):
        loop, *_ = tmploop
        exps = _make_experiences(5, quality=0.99)
        strategy = _make_strategy()
        step_data = {"weight": 0.5, "calls": 5, "ema_signal": 10.0, "last_updated": ""}
        exp = loop._run_experiment(
            target_type="error_spike",
            step_name="query_metrics",
            step_data=step_data,
            experiences=exps,
            working_strategy=strategy,
            min_delta=0.0,
        )
        assert exp.candidate_quality <= 1.0


# ---------------------------------------------------------------------------
# Full run_cycle integration
# ---------------------------------------------------------------------------

class TestRunCycle:
    def _write_files(self, tmp_path, exp_path, strat_path, exps, strategy):
        with open(exp_path, "w") as f:
            json.dump(exps, f)
        with open(strat_path, "w") as f:
            json.dump(strategy, f)

    def test_returns_improvement_report(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = _make_experiences(10, quality=0.50)
        strategy = _make_strategy()
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        report = loop.run_cycle(max_experiments=2, min_delta=0.01)
        assert isinstance(report, ImprovementReport)
        assert report.cycle_id  # non-empty

    def test_report_has_correct_baseline(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = _make_experiences(10, quality=0.60)
        strategy = _make_strategy()
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        report = loop.run_cycle(max_experiments=1, min_delta=0.50)  # high threshold → no accepts
        assert report.baseline_score == pytest.approx(0.60, abs=1e-3)

    def test_accepted_changes_written_to_strategy_file(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = _make_experiences(20, quality=0.50)
        strategy = _make_strategy()
        # Make query_metrics highly underweighted (ema=2.0, weight=0.5)
        strategy["error_spike"]["query_metrics"] = {
            "weight": 0.5, "calls": 50, "ema_signal": 2.0, "last_updated": ""
        }
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        report = loop.run_cycle(max_experiments=1, min_delta=0.01)
        if report.accepted_count > 0:
            with open(strat_path) as f:
                updated = json.load(f)
            assert updated["error_spike"]["query_metrics"]["weight"] > 0.5

    def test_no_experiments_if_no_strategy(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = _make_experiences(5, quality=0.50)
        with open(exp_path, "w") as f:
            json.dump(exps, f)
        # No strategy file → load returns {}
        report = loop.run_cycle()
        assert report.experiments_run == 0
        assert report.accepted_count == 0

    def test_no_experiments_if_no_experiences(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        with open(exp_path, "w") as f:
            json.dump([], f)
        with open(strat_path, "w") as f:
            json.dump(_make_strategy(), f)
        report = loop.run_cycle()
        assert report.total_count == 0
        assert report.experiments_run == 0

    def test_report_persisted_to_file(self, tmploop):
        loop, tmp_path, exp_path, strat_path, report_path = tmploop
        exps = _make_experiences(5, quality=0.55)
        strategy = _make_strategy()
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        loop.run_cycle(max_experiments=1, min_delta=0.99)  # no accepts but report saved
        assert os.path.exists(report_path)
        with open(report_path) as f:
            reports = json.load(f)
        assert len(reports) == 1
        assert "cycle_id" in reports[0]

    def test_multiple_cycles_append_reports(self, tmploop):
        loop, tmp_path, exp_path, strat_path, report_path = tmploop
        exps = _make_experiences(5, quality=0.55)
        strategy = _make_strategy()
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        loop.run_cycle(max_experiments=1, min_delta=0.99)
        loop.run_cycle(max_experiments=1, min_delta=0.99)
        with open(report_path) as f:
            reports = json.load(f)
        assert len(reports) == 2

    def test_worst_incident_type_in_report(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = (
            _make_experiences(5, incident_type="error_spike", quality=0.80)
            + _make_experiences(5, incident_type="error_spike", quality=0.40)
        )
        strategy = _make_strategy(incident_type="error_spike")
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        report = loop.run_cycle(max_experiments=1, min_delta=0.99)
        assert report.worst_incident_type == "error_spike"

    def test_report_to_dict_serialisable(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = _make_experiences(5, quality=0.55)
        strategy = _make_strategy()
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        report = loop.run_cycle(max_experiments=1, min_delta=0.99)
        d = report.to_dict()
        assert json.dumps(d)  # no serialisation error

    def test_failure_count_in_report(self, tmploop):
        loop, tmp_path, exp_path, strat_path, _ = tmploop
        exps = _make_experiences(3, quality=0.50) + _make_experiences(7, quality=0.80)
        strategy = _make_strategy()
        self._write_files(tmp_path, exp_path, strat_path, exps, strategy)
        report = loop.run_cycle(max_experiments=1, min_delta=0.99)
        assert report.failure_count == 3


# ---------------------------------------------------------------------------
# run_nightly_self_improvement integration
# ---------------------------------------------------------------------------

class TestNightlySelfImprovement:
    def test_self_improvement_key_in_summary(self, tmploop, tmp_path):
        loop, *_ = tmploop
        # Patch the module-level paths so nightly sees our temp files
        exps = _make_experiences(10, quality=0.55)
        strategy = _make_strategy()
        exp_path = str(tmp_path / "experience_store.json")
        strat_path = str(tmp_path / "evolved_strategy.json")
        with open(exp_path, "w") as f:
            json.dump(exps, f)
        with open(strat_path, "w") as f:
            json.dump(strategy, f)

        with (
            patch("supervisor.self_improvement_loop._EXPERIENCE_PATH", exp_path),
            patch("supervisor.self_improvement_loop._STRATEGY_PATH", strat_path),
            patch("supervisor.self_improvement_loop._loop", None),
        ):
            from supervisor.learning_loop import run_nightly_self_improvement
            summary = run_nightly_self_improvement()

        assert "self_improvement" in summary
        assert "cycle_id" in summary["self_improvement"]

    def test_nightly_never_raises(self, tmp_path):
        # Even with no data files, nightly should not raise
        with (
            patch("supervisor.self_improvement_loop._EXPERIENCE_PATH", str(tmp_path / "missing.json")),
            patch("supervisor.self_improvement_loop._STRATEGY_PATH", str(tmp_path / "missing.json")),
            patch("supervisor.self_improvement_loop._loop", None),
        ):
            from supervisor.learning_loop import run_nightly_self_improvement
            result = run_nightly_self_improvement()
        assert isinstance(result, dict)
