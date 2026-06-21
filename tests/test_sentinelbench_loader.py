import json
import pytest
import yaml
from pathlib import Path
from sentinelbench.loader import ScenarioLoader


SCENARIO_YML = {
    "schema_version": "1.0",
    "scenario_id": "test-001",
    "title": "Test Scenario",
    "failure_mode": "timeout",
    "severity": "p1",
    "affected_service": "test-service",
    "difficulty": "medium",
    "available_evidence": ["splunk_logs", "metrics"],
}

ANSWER_YML = {
    "schema_version": "1.0",
    "root_cause_category": "connection_pool_exhaustion",
    "required_keywords": ["connection pool", "postgres"],
    "required_evidence_sources": ["splunk_logs"],
}

ALERT_JSON = {"alert_id": "A-001", "severity": "critical"}


def write_scenario(path: Path, scenario=None, answer=None, alert=None, evidence=None):
    path.mkdir(parents=True, exist_ok=True)
    (path / "scenario.yml").write_text(yaml.dump(scenario or SCENARIO_YML))
    (path / "answer.yml").write_text(yaml.dump(answer or ANSWER_YML))
    (path / "alert.json").write_text(json.dumps(alert or ALERT_JSON))
    if evidence is not None:
        ev_dir = path / "evidence"
        ev_dir.mkdir(exist_ok=True)
        for name, data in evidence.items():
            (ev_dir / f"{name}.json").write_text(json.dumps(data))


def test_load_returns_tuple_of_4(tmp_path):
    write_scenario(tmp_path / "s1")
    loader = ScenarioLoader()
    result = loader.load(tmp_path / "s1")
    assert len(result) == 4


def test_load_parses_scenario(tmp_path):
    write_scenario(tmp_path / "s1")
    loader = ScenarioLoader()
    scenario, _, _, _ = loader.load(tmp_path / "s1")
    assert scenario.scenario_id == "test-001"
    assert scenario.affected_service == "test-service"


def test_load_parses_expected_answer(tmp_path):
    write_scenario(tmp_path / "s1")
    loader = ScenarioLoader()
    _, expected, _, _ = loader.load(tmp_path / "s1")
    assert expected.root_cause_category == "connection_pool_exhaustion"


def test_load_parses_alert(tmp_path):
    write_scenario(tmp_path / "s1")
    loader = ScenarioLoader()
    _, _, alert, _ = loader.load(tmp_path / "s1")
    assert alert["alert_id"] == "A-001"


def test_load_evidence_keys_are_file_stems(tmp_path):
    write_scenario(tmp_path / "s1", evidence={"splunk_logs": {"key": "val"}, "metrics": [1, 2, 3]})
    loader = ScenarioLoader()
    _, _, _, evidence = loader.load(tmp_path / "s1")
    assert "splunk_logs" in evidence
    assert "metrics" in evidence


def test_load_raises_if_required_evidence_not_in_available(tmp_path):
    answer = dict(ANSWER_YML)
    answer["required_evidence_sources"] = ["missing_source"]
    write_scenario(tmp_path / "s1", answer=answer)
    loader = ScenarioLoader()
    with pytest.raises(ValueError, match="required_evidence_sources"):
        loader.load(tmp_path / "s1")


def test_load_handles_missing_evidence_dir(tmp_path):
    write_scenario(tmp_path / "s1")
    loader = ScenarioLoader()
    _, _, _, evidence = loader.load(tmp_path / "s1")
    assert evidence == {}


def test_load_all_returns_sorted_by_name(tmp_path):
    for name in ["003-c", "001-a", "002-b"]:
        scenario = dict(SCENARIO_YML)
        scenario["scenario_id"] = name
        write_scenario(tmp_path / name, scenario=scenario)
    loader = ScenarioLoader()
    results = loader.load_all(tmp_path)
    ids = [r[0].scenario_id for r in results]
    assert ids == ["001-a", "002-b", "003-c"]


def test_load_all_skips_non_scenario_dirs(tmp_path):
    write_scenario(tmp_path / "001-valid")
    (tmp_path / "not-a-scenario").mkdir()
    loader = ScenarioLoader()
    results = loader.load_all(tmp_path)
    assert len(results) == 1
