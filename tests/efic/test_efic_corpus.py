"""EFIC — Enterprise Failure Intelligence Corpus tests.

Validates the corpus is deterministic, EB-0-runnable, deduplicated by reasoning
problem, and that every scenario satisfies the quality gates (known ground
truth, >=2 required MCPs, negative evidence, red herrings, reasoning contract).
Coverage is asserted honestly — gaps are reported, not hidden.
"""
from __future__ import annotations

import json
import os

from eval.efic.build_corpus import MCPS, TAXONOMY, build_corpus
from enterprisebench.loader import validate_corpus
from enterprisebench.runner import NOT_MEASURED, run

_CORPUS_JSON = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "eval", "efic", "corpus.json")


class TestDeterminismAndReuse:
    def test_deterministic_rebuild(self):
        assert json.dumps(build_corpus(), sort_keys=True) == \
            json.dumps(build_corpus(), sort_keys=True)

    def test_committed_file_matches_build(self):
        with open(_CORPUS_JSON) as f:
            committed = json.load(f)
        assert committed == build_corpus()

    def test_eic_compatible_content_addressed(self):
        for e in build_corpus()["corpus"]:
            assert len(e["task"]["task_hash"]) == 16
            # replay seed is the content-addressed task hash (deterministic)
            assert e["efic"]["replay_seed"] == e["task"]["task_hash"]

    def test_eb0_validates_and_runs(self):
        c = build_corpus()
        assert len(validate_corpus(c)) == c["coverage"]["scenarios"]
        # runs through EB-0 offline; no engine submissions -> NOT_MEASURED
        report = run(_CORPUS_JSON)
        assert report["counts"][NOT_MEASURED] == report["scenario_count"]


class TestDeduplication:
    def test_no_duplicate_reasoning_problem(self):
        ent = build_corpus()["corpus"]
        keys = [(e["efic"]["failure_family"], e["efic"]["failure_mode"],
                 e["efic"]["reasoning_category"]) for e in ent]
        assert len(keys) == len(set(keys))     # every scenario is distinct

    def test_unique_scenario_ids(self):
        ids = [e["task"]["task_id"] for e in build_corpus()["corpus"]]
        assert len(ids) == len(set(ids))


class TestQualityGates:
    def test_every_scenario_has_known_ground_truth(self):
        for e in build_corpus()["corpus"]:
            gt = e["task"]["ground_truth"]
            assert gt["root_cause"] and gt["root_cause_service"]
            assert gt["necessary_evidence"] and gt["decisive_evidence"]

    def test_cross_mcp_at_least_two_required(self):
        for e in build_corpus()["corpus"]:
            required = [m for m, u in e["efic"]["mcp_utilization"].items()
                        if u == "required"]
            assert len(required) >= 2, e["efic"]["scenario_id"]

    def test_negative_evidence_and_red_herrings_present(self):
        for e in build_corpus()["corpus"]:
            assert e["efic"]["negative_evidence"], e["efic"]["scenario_id"]
            assert e["efic"]["red_herrings"], e["efic"]["scenario_id"]

    def test_reasoning_contract_present(self):
        for e in build_corpus()["corpus"]:
            ef = e["efic"]
            assert ef["hypotheses_considered"] and ef["hypotheses_eliminated"]
            assert ef["reasoning_category"] and ef["contributing_factors"]

    def test_confidence_range_and_owner_recommendation(self):
        for e in build_corpus()["corpus"]:
            lo, hi = e["efic"]["expected_confidence_range"]
            assert 0 <= lo <= hi <= 100
            assert e["efic"]["expected_owner"] and e["efic"]["expected_recommendation"]

    def test_mcp_utilization_values_valid(self):
        allowed = {"required", "optional", "expected_empty", "not_applicable"}
        for e in build_corpus()["corpus"]:
            u = e["efic"]["mcp_utilization"]
            assert set(u) == set(MCPS)               # every MCP declared
            assert set(u.values()) <= allowed


class TestCoverage:
    def test_coverage_model_present_and_honest(self):
        cov = build_corpus()["coverage"]
        assert cov["scenarios"] == 16
        # families covered are a subset of the taxonomy
        assert set(cov["failure_families_covered"]).issubset(set(TAXONOMY))
        # gaps are reported (not hidden) — the foundational set is not complete
        assert cov["failure_modes_gaps"], "gaps must be reported honestly"
        # every scenario carries negative evidence + red herrings
        assert cov["scenarios_with_negative_evidence"] == 16
        assert cov["scenarios_with_red_herrings"] == 16

    def test_taxonomy_modes_are_distinct(self):
        for fam, modes in TAXONOMY.items():
            assert len(modes) == len(set(modes)), fam
