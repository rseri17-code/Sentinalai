"""SentinelBench — runner + report tests.

Verifies the runner and report renderer are deterministic and never
touch external systems.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.synthetic.report import REPORT_SCHEMA_VERSION, render_report, render_report_json
from tests.synthetic.runner import (
    SCENARIOS_DIR,
    load_all_scenarios,
    load_scenario,
    run_all_scenarios,
    run_scenario,
)
from tests.synthetic.schemas import ScenarioSchemaError


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class TestLoad:
    def test_load_scenario_by_id(self):
        sc = load_scenario("k8s_pod_crashloop")
        assert sc.scenario_id == "k8s_pod_crashloop"

    def test_load_scenario_unknown(self):
        with pytest.raises(ScenarioSchemaError):
            load_scenario("does_not_exist")

    def test_load_all_returns_dict_sorted_by_id(self):
        d = load_all_scenarios()
        assert list(d.keys()) == sorted(d.keys())
        assert len(d) == 5
        for k, v in d.items():
            assert v.scenario_id == k

    def test_load_all_from_custom_dir(self, tmp_path):
        d = load_all_scenarios(scenarios_dir=tmp_path)
        assert d == {}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TestRun:
    def test_run_scenario_by_id_perfect(self):
        card = run_scenario("k8s_pod_crashloop")
        # Every scenario ships a perfect mock output
        assert card.overall_score == 1.0
        assert card.scenario_id == "k8s_pod_crashloop"

    def test_run_scenario_with_external_output(self):
        card = run_scenario("k8s_pod_crashloop", investigation_output={
            "root_cause": "totally wrong",
            "confidence": 10,
            "evidence_keys": [],
            "decision_signals": [],
            "mtti_ms": 900000,
            "runtime_cost": 999,
        })
        assert card.overall_score < 0.3

    def test_run_all_scenarios_returns_five_cards(self):
        cards = run_all_scenarios()
        assert len(cards) == 5
        ids = [c.scenario_id for c in cards]
        assert ids == sorted(ids)

    def test_run_all_scenarios_perfect_scores(self):
        cards = run_all_scenarios()
        for c in cards:
            assert c.overall_score == 1.0, c.scenario_id

    def test_run_all_scenarios_with_selective_outputs(self):
        io_map = {
            "k8s_pod_crashloop": {
                "root_cause": "wrong",
                "confidence": 5, "evidence_keys": [],
                "decision_signals": [], "mtti_ms": 999999, "runtime_cost": 999,
            },
        }
        cards = run_all_scenarios(investigation_outputs=io_map)
        by_id = {c.scenario_id: c for c in cards}
        # The overridden one is degraded
        assert by_id["k8s_pod_crashloop"].overall_score < 0.3
        # The others still score perfectly against their mock outputs
        for other_id in ("bad_deployment_5xx", "database_latency_saturation",
                          "dns_resolution_failure",
                          "auth_token_validation_failure"):
            assert by_id[other_id].overall_score == 1.0


# ---------------------------------------------------------------------------
# Determinism (same-input → byte-identical output)
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_run_all_scenarios_deterministic(self):
        cards1 = run_all_scenarios()
        cards2 = run_all_scenarios()
        j1 = json.dumps([c.to_dict() for c in cards1], sort_keys=True)
        j2 = json.dumps([c.to_dict() for c in cards2], sort_keys=True)
        assert j1 == j2

    def test_render_report_deterministic(self):
        cards = run_all_scenarios()
        s1 = render_report_json(cards)
        s2 = render_report_json(cards)
        assert s1 == s2


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReport:
    def test_render_report_shape(self):
        cards = run_all_scenarios()
        report = render_report(cards)
        assert report["schema_version"] == REPORT_SCHEMA_VERSION
        assert report["scorecard_count"] == 5
        assert "aggregates" in report
        assert "overall_mean" in report["aggregates"]
        assert "per_dimension_mean" in report["aggregates"]
        assert report["aggregates"]["overall_mean"] == 1.0
        assert report["aggregates"]["overall_min"] == 1.0
        assert report["aggregates"]["overall_max"] == 1.0

    def test_render_report_json_parses_back(self):
        cards = run_all_scenarios()
        s = render_report_json(cards)
        d = json.loads(s)
        assert d["scorecard_count"] == 5

    def test_render_report_empty(self):
        report = render_report([])
        assert report["scorecard_count"] == 0
        assert report["aggregates"]["overall_mean"] == 0.0

    def test_report_scorecards_sorted_by_scenario_id(self):
        cards = run_all_scenarios()
        # Reverse-shuffle and re-render
        shuffled = list(reversed(cards))
        report = render_report(shuffled)
        ids = [c["scenario_id"] for c in report["scorecards"]]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Isolation — no external systems, no production mutation
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_runner_module_has_no_network_imports(self):
        """Ensure the runner never imports a network client."""
        import tests.synthetic.runner as m
        src = open(m.__file__).read()
        for banned in ("requests", "httpx", "urllib3", "boto3", "openai",
                         "anthropic", "kubernetes", "prometheus_api"):
            assert banned not in src, f"runner imports {banned}"

    def test_scoring_module_has_no_network_imports(self):
        import tests.synthetic.scoring as m
        src = open(m.__file__).read()
        for banned in ("requests", "httpx", "urllib3", "boto3", "openai",
                         "anthropic"):
            assert banned not in src, f"scoring imports {banned}"

    def test_scenarios_directory_is_local(self):
        assert SCENARIOS_DIR.exists()
        assert SCENARIOS_DIR.is_dir()

    def test_does_not_touch_investigate(self):
        """Verify SentinelBench never imports supervisor.agent."""
        import tests.synthetic.runner as m1
        import tests.synthetic.scoring as m2
        import tests.synthetic.schemas as m3
        for m in (m1, m2, m3):
            assert "supervisor.agent" not in open(m.__file__).read()
