import json
import yaml
import pytest
from pathlib import Path
from sentinelbench.runner import BenchRunner
from sentinelbench.schema import Scenario, ExpectedAnswer, RCAScore


SCENARIO_DATA = {
    "schema_version": "1.0",
    "scenario_id": "test-001",
    "title": "Test",
    "failure_mode": "timeout",
    "severity": "p1",
    "affected_service": "test-service",
    "difficulty": "medium",
    "available_evidence": ["splunk_logs", "metrics"],
}

ANSWER_DATA = {
    "schema_version": "1.0",
    "root_cause_category": "connection_pool_exhaustion",
    "required_keywords": ["connection pool"],
    "required_evidence_sources": ["splunk_logs"],
}

ALERT = {"alert_id": "A-001"}


def write_scenario(path: Path, scenario_id="test-001"):
    path.mkdir(parents=True, exist_ok=True)
    s = dict(SCENARIO_DATA)
    s["scenario_id"] = scenario_id
    (path / "scenario.yml").write_text(yaml.dump(s))
    (path / "answer.yml").write_text(yaml.dump(ANSWER_DATA))
    (path / "alert.json").write_text(json.dumps(ALERT))
    ev = path / "evidence"
    ev.mkdir(exist_ok=True)
    (ev / "splunk_logs.json").write_text(json.dumps({
        "message": "connection pool exhausted — postgres max_connections reached"
    }))


def test_run_scenario_with_fixture_returns_rca_score():
    runner = BenchRunner(ci_mode=True)
    scenario = Scenario(**SCENARIO_DATA)
    expected = ExpectedAnswer(**ANSWER_DATA)
    evidence = {"splunk_logs": {"message": "connection pool issue"}}
    score = runner.run_scenario_with_fixture(scenario, expected, ALERT, evidence)
    assert isinstance(score, RCAScore)


def test_run_scenario_fixture_result_has_required_keys():
    runner = BenchRunner(ci_mode=True)
    scenario = Scenario(**SCENARIO_DATA)
    evidence = {"splunk_logs": {"message": "test"}}
    result = runner._build_fixture_result(scenario, evidence)
    for key in ["root_cause", "summary", "confidence", "recommended_action", "playbook", "tools_called"]:
        assert key in result


def test_build_fixture_result_recommended_action_non_empty():
    runner = BenchRunner(ci_mode=True)
    scenario = Scenario(**SCENARIO_DATA)
    result = runner._build_fixture_result(scenario, {})
    assert result["recommended_action"] != ""


def test_run_all_returns_scorecard(tmp_path):
    write_scenario(tmp_path / "001-a")
    write_scenario(tmp_path / "002-b", scenario_id="test-002")
    runner = BenchRunner(ci_mode=True)
    card = runner.run_all(str(tmp_path))
    assert card.total_scenarios == 2
    assert len(card.scores) == 2


def test_run_all_scorecard_pass_rate_consistent(tmp_path):
    write_scenario(tmp_path / "001-a")
    write_scenario(tmp_path / "002-b", scenario_id="test-002")
    runner = BenchRunner(ci_mode=True)
    card = runner.run_all(str(tmp_path))
    assert card.passed + card.failed == card.total_scenarios
    assert card.pass_rate == pytest.approx(card.passed / card.total_scenarios)


def test_run_all_scorecard_run_id_non_empty(tmp_path):
    write_scenario(tmp_path / "001-a")
    runner = BenchRunner(ci_mode=True)
    card = runner.run_all(str(tmp_path))
    assert len(card.run_id) > 0
    assert card.timestamp != ""
