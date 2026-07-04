"""SentinelBench — schema validation tests.

Verifies every scenario in the corpus is well-formed and that
:class:`Scenario.from_dict` catches every documented failure mode.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.synthetic.runner import SCENARIOS_DIR
from tests.synthetic.schemas import (
    SCENARIO_SCHEMA_VERSION,
    Scenario,
    ScenarioSchemaError,
    validate_scenario_dict,
)


# ---------------------------------------------------------------------------
# Corpus scan
# ---------------------------------------------------------------------------

def _all_scenario_paths() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.json"))


class TestCorpus:
    def test_corpus_has_five_scenarios(self):
        paths = _all_scenario_paths()
        assert len(paths) == 5, [p.name for p in paths]

    @pytest.mark.parametrize("path", _all_scenario_paths(),
                              ids=[p.stem for p in _all_scenario_paths()])
    def test_every_scenario_is_valid(self, path):
        scenario = Scenario.from_json_file(path)
        # Filename stem must match scenario_id
        assert scenario.scenario_id == path.stem
        # Schema version stamped
        assert scenario.schema_version == SCENARIO_SCHEMA_VERSION

    @pytest.mark.parametrize("path", _all_scenario_paths(),
                              ids=[p.stem for p in _all_scenario_paths()])
    def test_every_scenario_has_mock_output(self, path):
        """Every corpus scenario ships with a mock_investigation_output
        so CI runs are deterministic."""
        scenario = Scenario.from_json_file(path)
        assert scenario.mock_investigation_output
        for key in ("root_cause", "confidence", "evidence_keys",
                     "decision_signals", "mtti_ms", "runtime_cost"):
            assert key in scenario.mock_investigation_output, key


# ---------------------------------------------------------------------------
# from_dict — happy path
# ---------------------------------------------------------------------------

VALID = {
    "scenario_id": "unit_test_scenario",
    "title": "Unit test scenario",
    "incident_input": {"incident_id": "T1", "service": "checkout"},
    "mocked_evidence_sources": {"logs": []},
    "expected_root_cause": "pool exhausted",
    "required_evidence": ["logs"],
    "red_herrings": ["deployment"],
    "expected_confidence_range": [60, 80],
    "expected_decision_signals": ["have_prior_resolution_memory"],
    "expected_mtti_budget_ms": 60000,
    "expected_runtime_cost_budget": 20,
    "tags": ["unit"],
}


class TestValidSchema:
    def test_from_dict_succeeds(self):
        sc = Scenario.from_dict(VALID)
        assert sc.scenario_id == "unit_test_scenario"
        assert sc.expected_confidence_range == (60, 80)
        assert sc.required_evidence == ("logs",)
        assert sc.red_herrings == ("deployment",)
        assert sc.tags == ("unit",)

    def test_to_dict_is_json_safe(self):
        sc = Scenario.from_dict(VALID)
        d = sc.to_dict()
        json.dumps(d)   # must not raise

    def test_range_swapped_normalises(self):
        bad = copy.deepcopy(VALID)
        # Range endpoints in ascending order per schema — swapped
        # endpoints are rejected at validation time.
        bad["expected_confidence_range"] = [80, 60]
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_frozen(self):
        sc = Scenario.from_dict(VALID)
        with pytest.raises(Exception):
            sc.scenario_id = "other"


# ---------------------------------------------------------------------------
# validate_scenario_dict — failure modes
# ---------------------------------------------------------------------------

class TestFailureModes:
    def test_missing_required_key(self):
        bad = copy.deepcopy(VALID)
        del bad["expected_root_cause"]
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_empty_scenario_id(self):
        bad = copy.deepcopy(VALID)
        bad["scenario_id"] = ""
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_wrong_type_incident_input(self):
        bad = copy.deepcopy(VALID)
        bad["incident_input"] = "not-a-dict"
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_range_out_of_bounds(self):
        bad = copy.deepcopy(VALID)
        bad["expected_confidence_range"] = [10, 150]
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_range_wrong_length(self):
        bad = copy.deepcopy(VALID)
        bad["expected_confidence_range"] = [60]
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_negative_mtti_budget(self):
        bad = copy.deepcopy(VALID)
        bad["expected_mtti_budget_ms"] = -1
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_negative_cost_budget(self):
        bad = copy.deepcopy(VALID)
        bad["expected_runtime_cost_budget"] = -1
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_dict(bad)

    def test_bad_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not-json")
        with pytest.raises(ScenarioSchemaError):
            Scenario.from_json_file(p)

    def test_top_level_not_dict(self):
        with pytest.raises(ScenarioSchemaError):
            validate_scenario_dict([1, 2, 3])
